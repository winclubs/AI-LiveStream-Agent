"""编排器接入句级前瞻预取的回归测试 (P0-1)。

锁定两条关键行为：
1. 批处理 Provider 命中预取缓存时，编排器**不得**再发起云端批处理请求
   （否则预取毫无意义，且会重复上屏）；
2. 未命中缓存时必须照常派发，绝不因预取层存在而改变既有失败语义。
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from server.adapters.media.avatar_orchestrator import (
    AvatarProviderEntry,
    AvatarProviderOrchestrator,
    AvatarProviderPolicy,
)
from server.adapters.media.avatar_provider import (
    AvatarProviderCapabilities,
    AvatarRenderMode,
    ProviderMode,
    ProviderRenderResult,
    ProviderVerification,
)


def _result(audio_id: str, frames: int = 3) -> ProviderRenderResult:
    return ProviderRenderResult(
        provider_id="batch_p",
        request_id="req_1",
        audio_id=audio_id,
        render_mode=AvatarRenderMode.BATCH,
        latency_ms=1234.0,
        output_frames=frames,
    )


def _frames(audio_id: str):
    from server.core.media.audio_frame import AudioFrame, AudioFormat

    return [
        AudioFrame(
            data=b"\x00\x00" * 640,
            format=AudioFormat(sample_rate=16000, channels=1),
            audio_id=audio_id,
            sequence=0,
            pts_samples=0,
            audio_generation=1,
            session_generation=1,
            text="预取测试",
            is_first=True,
            is_final=True,
        )
    ]


class _BatchProvider:
    """真实类定义的批处理 Provider 替身（切片缓存接口必须在类上）。"""

    provider_id = "batch_p"
    display_name = "Batch Provider"

    def __init__(self):
        self.render_calls = 0
        self.played: list[str] = []
        self._cache: dict[str, list[bytes]] = {}

    @property
    def capabilities(self):
        # 与真实 LatentSyncBatchAvatarProvider 保持一致的能力声明：
        # BATCH 模式必须满足严格自动承接边界（已验证 + 整句事务 + 取消 ACK 等）。
        return AvatarProviderCapabilities(
            render_modes=frozenset({AvatarRenderMode.BATCH}),
            input_codecs=frozenset({"pcm_s16le"}),
            renderer_only=True,
            neural_lipsync=True,
            transactional_sentence=True,
            supports_cancel=True,
            supports_cancel_ack=True,
            strict_completion=True,
            protocol_name="latentsync_batch_v1",
            protocol_version=1,
            verification=ProviderVerification.VERIFIED,
        )

    def seed(self, audio_id: str, count: int = 3):
        self._cache[audio_id] = [b"jpeg"] * count

    def get_cached_slice(self, audio_id: str):
        return list(self._cache.get(audio_id) or ())

    def clear_slice_cache(self):
        self._cache.clear()

    async def play_cached_slice(self, audio_id: str) -> bool:
        if not self._cache.pop(audio_id, None):
            return False
        self.played.append(audio_id)
        return True

    async def render_sentence(self, frames, *, render_mode, publish=True):
        self.render_calls += 1
        return _result(frames[0].audio_id)

    async def interrupt(self, reason="", *, next_generation=None):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None

    def get_status(self):
        return {"provider_id": self.provider_id}


class _Prefetcher:
    def __init__(self, provider, ready_ids=()):
        self._provider = provider
        self._ready = set(ready_ids)
        self.requested: list[str] = []
        self.cleared = 0

    async def prefetch_sentence(self, audio_id, frames, text=""):
        self.requested.append(audio_id)

    async def acquire_slice(self, audio_id):
        if audio_id in self._ready:
            self._ready.discard(audio_id)
            return MagicMock(result=_result(audio_id))
        return None

    async def interrupt_and_clear(self):
        self.cleared += 1
        self._ready.clear()
        self._provider.clear_slice_cache()

    async def close(self):
        return None

    @property
    def ready_count(self):
        return len(self._ready)

    @property
    def rendering_count(self):
        return 0


def _entry(provider) -> AvatarProviderEntry:
    return AvatarProviderEntry(
        provider=provider,
        policy=AvatarProviderPolicy(mode=ProviderMode.PRIMARY),
    )


class TestOrchestratorPrefetch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.provider = _BatchProvider()
        self.orchestrator = AvatarProviderOrchestrator([_entry(self.provider)])
        await self.orchestrator.start()

    async def asyncTearDown(self):
        await self.orchestrator.stop()

    async def test_batch_providers_detected(self):
        found = self.orchestrator.batch_providers()
        self.assertEqual([p.provider_id for p in found], ["batch_p"])

    async def test_prefetch_hit_skips_cloud_render(self):
        prefetcher = _Prefetcher(self.provider, ready_ids={"aud_hit"})
        self.orchestrator.attach_prefetcher(prefetcher)

        outcome = await self.orchestrator.dispatch_sentence(_frames("aud_hit"))

        self.assertFalse(outcome.fallback_reason)
        self.assertEqual(self.provider.render_calls, 0, "命中缓存不得再请求云端")
        self.assertEqual(self.orchestrator.prefetch_hits, 1)

    async def test_prefetch_miss_falls_through_to_render(self):
        prefetcher = _Prefetcher(self.provider, ready_ids=set())
        self.orchestrator.attach_prefetcher(prefetcher)

        outcome = await self.orchestrator.dispatch_sentence(_frames("aud_miss"))

        self.assertEqual(self.provider.render_calls, 1, "未命中必须照常派发")
        self.assertFalse(outcome.fallback_reason)

    async def test_prefetch_upcoming_forwards_to_prefetcher(self):
        prefetcher = _Prefetcher(self.provider)
        self.orchestrator.attach_prefetcher(prefetcher)

        await self.orchestrator.prefetch_upcoming(_frames("aud_next"))
        self.assertEqual(prefetcher.requested, ["aud_next"])

    async def test_prefetch_upcoming_is_noop_without_prefetcher(self):
        # 无预取器时必须是彻底空转，不得抛错影响主链路
        await self.orchestrator.prefetch_upcoming(_frames("aud_x"))

    async def test_prefetch_failure_does_not_break_dispatch(self):
        class _Broken(_Prefetcher):
            async def acquire_slice(self, audio_id):
                raise RuntimeError("cache backend down")

        self.orchestrator.attach_prefetcher(_Broken(self.provider))
        outcome = await self.orchestrator.dispatch_sentence(_frames("aud_brk"))
        self.assertEqual(self.provider.render_calls, 1)
        self.assertFalse(outcome.fallback_reason)


if __name__ == "__main__":
    unittest.main()
