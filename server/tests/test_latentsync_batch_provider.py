"""LatentSync 批处理预渲染 Provider (路径二) 自动化测试。"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from server.adapters.media.avatar_provider import AvatarRenderMode, ProviderRenderResult
from server.adapters.media.avatar_provider_registry import (
    create_avatar_provider,
    get_avatar_provider_descriptor,
    list_avatar_provider_descriptors,
)
from server.adapters.media.avatar_provider import ProviderVerification
from server.adapters.media.latentsync_batch_provider import LatentSyncBatchAvatarProvider
from server.core.avatar.slice_prefetcher import PrefetchedSlice, SlicePrefetcher, SliceState
from server.core.media.audio_frame import AudioFormat, AudioFrame


def _make_dummy_frame(audio_id: str, seq: int) -> AudioFrame:
    # 16000Hz, 单声道 16bit, 40ms = 640 samples = 1280 bytes
    pcm_data = b"\x00\x00" * 640
    return AudioFrame(
        data=pcm_data,
        format=AudioFormat(codec="pcm_s16le", sample_rate=16000, channels=1),
        audio_id=audio_id,
        sequence=seq,
        pts_samples=seq * 640,
        audio_generation=1,
        session_generation=1,
        is_first=(seq == 0),
        is_final=False,
    )


class TestLatentSyncBatchProvider(unittest.IsolatedAsyncioTestCase):
    def test_descriptor_registration(self):
        desc = get_avatar_provider_descriptor("latentsync_batch")
        self.assertIsNotNone(desc)
        self.assertEqual(desc.adapter_id, "latentsync_batch")
        self.assertTrue(desc.selectable)
        self.assertIn(AvatarRenderMode.BATCH.value, desc.capabilities["render_modes"])

    def test_provider_capabilities(self):
        provider = LatentSyncBatchAvatarProvider(
            provider_id="test_latentsync",
            display_name="测试 LatentSync 节点",
            node_url="https://gpu.gongying.bond",
        )
        self.assertIn(AvatarRenderMode.BATCH, provider.capabilities.render_modes)
        self.assertTrue(provider.capabilities.neural_lipsync)
        self.assertEqual(provider.capabilities.protocol_name, "latentsync_batch_v1")

        # 可选性必须跟随实测探活：新建（未探活）时不得宣称可选 (ADR-16)。
        # 编排器 `avatar_orchestrator._choose_render_mode` 在 start() 成功、
        # lifecycle_started=True 之后才咨询可选性，故此语义与编排器一致。
        self.assertFalse(provider.capabilities.is_mode_eligible(AvatarRenderMode.BATCH))
        provider._is_ready = True
        self.assertTrue(provider.capabilities.is_mode_eligible(AvatarRenderMode.BATCH))

    def test_verification_reflects_runtime_probe_state(self):
        """verification 必须随探活结果动态变化 (ADR-16)。

        历史缺陷：`verification=ProviderVerification.VERIFIED` 是无条件硬编码，
        对象一经构造即向状态页宣告"已验证神经唇形"，与节点是否可达/就绪无关。
        未探活 (start() 尚未成功) 时必须如实为 UNVERIFIED。
        """
        provider = LatentSyncBatchAvatarProvider(
            provider_id="test_latentsync_verify",
            display_name="测试 LatentSync 验证态",
            node_url="https://gpu.gongying.bond",
        )
        # 初始态：尚未探活成功
        self.assertFalse(provider._is_ready)
        self.assertEqual(
            provider.capabilities.verification,
            ProviderVerification.UNVERIFIED,
            "未探活成功时不得宣称 VERIFIED",
        )

        # 探活成功后就绪
        provider._is_ready = True
        self.assertEqual(
            provider.capabilities.verification,
            ProviderVerification.VERIFIED,
            "探活成功后应如实反映为 VERIFIED",
        )

        # 探活失败回落后必须退回 UNVERIFIED
        provider._is_ready = False
        self.assertEqual(provider.capabilities.verification, ProviderVerification.UNVERIFIED)

    def test_wav_conversion(self):
        provider = LatentSyncBatchAvatarProvider(
            provider_id="test_latentsync",
            display_name="测试 LatentSync 节点",
            node_url="https://gpu.gongying.bond",
        )
        frames = [_make_dummy_frame("aud_001", i) for i in range(5)]
        wav_bytes = provider._convert_frames_to_wav_bytes(frames)
        self.assertGreater(len(wav_bytes), 44)  # 包含标准 WAV 头
        self.assertEqual(wav_bytes[:4], b"RIFF")
        self.assertEqual(wav_bytes[8:12], b"WAVE")

    async def test_create_avatar_provider_factory(self):
        extra_params = {
            "adapter": "latentsync_batch",
            "avatar_id": "default",
            "inference_steps": 20,
            "guidance_scale": 1.0,
        }
        provider, policy = create_avatar_provider(
            provider_instance_id="inst_001",
            display_name="LatentSync 批处理",
            base_url="https://gpu.gongying.bond",
            credential="test_secret_token",
            model_name="latentsync",
            extra_params=extra_params,
        )
        self.assertIsInstance(provider, LatentSyncBatchAvatarProvider)
        self.assertEqual(provider.provider_id, "neural_renderer:inst_001")
        self.assertEqual(policy.render_mode, AvatarRenderMode.BATCH)

    async def test_slice_prefetcher_flow(self):
        mock_provider = MagicMock(spec=LatentSyncBatchAvatarProvider)
        mock_provider.render_sentence = AsyncMock(
            return_value=ProviderRenderResult(
                provider_id="test_mock",
                request_id="req_001",
                audio_id="aud_slice_1",
                render_mode=AvatarRenderMode.BATCH,
                latency_ms=120.0,
                output_frames=25,
            )
        )
        mock_provider.interrupt = AsyncMock()

        prefetcher = SlicePrefetcher(mock_provider, lookahead_depth=2)
        frames = [_make_dummy_frame("aud_slice_1", 0)]

        # 触发预取
        await prefetcher.prefetch_sentence("aud_slice_1", frames, "你好，欢迎来到直播间")
        await asyncio.sleep(0.05)

        # 校验就绪状态
        self.assertEqual(prefetcher.ready_count, 1)
        slice_obj = await prefetcher.acquire_slice("aud_slice_1")
        self.assertIsNotNone(slice_obj)
        self.assertEqual(slice_obj.state, SliceState.CONSUMED)

        # 测试突发打断
        await prefetcher.interrupt_and_clear()
        mock_provider.interrupt.assert_awaited()
        self.assertEqual(prefetcher.ready_count, 0)


if __name__ == "__main__":
    unittest.main()
