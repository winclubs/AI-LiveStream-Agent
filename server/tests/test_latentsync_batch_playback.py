"""LatentSync BATCH 路径的实时切片播放时间轴与前瞻预取回归测试。

背景 (P0)：LatentSync 是扩散模型 (UNet3D, 20 步去噪)，无法逐帧流式推流，
必须「整句渲染 -> 拿到全部 JPEG -> 按音频播放头实时上屏」。

历史缺陷有两个，都属于「用户以为在播云端高清，实际不是」的静默失真：

1. `render_sentence()` 渲染完成后只把最后一帧写进 `_latest_jpeg` 供 MJPEG 预览，
   **从不向 frame_bus 发布任何帧** —— 于是 RTMP 公网推流 / 虚拟摄像头 / 录制器
   在整个句子期间始终停留在本地 procedural shadow 画面。云端算力白烧。
2. `SlicePrefetcher` 已实现前瞻预取，却从未被生产链路调用；批处理路径既没有
   预取带来的提前量，也没有按音频头对齐的时间轴，因此开箱必然音画失步。

本文件锁定修复后的行为契约。
"""

import asyncio
import base64
import unittest

import numpy as np

from server.adapters.media.latentsync_batch_playback import (
    PLAYBACK_FALLBACK_REASONS,
)
from server.adapters.media.avatar_provider import AvatarRenderMode
from server.adapters.media.latentsync_batch_provider import LatentSyncBatchAvatarProvider
from server.core.media.audio_frame import AudioFrame, AudioFormat


def _jpeg_bytes(color=(10, 20, 30)) -> bytes:
    """生成一张结构完整的真实 JPEG（避免依赖云端）。"""
    import cv2

    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[:, :] = color
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _audio_frames(audio_id: str, duration_ms: int = 200, sample_rate: int = 16000):
    frame_samples = sample_rate // 25  # 25 FPS 一帧
    total = int(sample_rate * duration_ms / 1000)
    pcm = np.zeros(total, dtype="<i2").tobytes()
    frames = []
    pts = 0
    seq = 0
    while pts + frame_samples <= total:
        chunk = pcm[pts * 2 : (pts + frame_samples) * 2]
        frames.append(
            AudioFrame(
                data=chunk,
                format=AudioFormat(sample_rate=sample_rate, channels=1),
                audio_id=audio_id,
                sequence=seq,
                pts_samples=pts,
                audio_generation=1,
                session_generation=1,
                text="测试" if seq == 0 else "",
                is_first=seq == 0,
                is_final=False,
            )
        )
        pts += frame_samples
        seq += 1
    return frames


def _clock(samples_played: int, *, finished: bool = False):
    return {
        "has_started": True,
        "samples_played": samples_played,
        "elapsed_sec": samples_played / 16000.0,
        "is_interrupted": False,
        "is_rejected": False,
        "is_finished": finished,
        "reject_reason": "",
    }


class _WallClock:
    """按真实墙钟推进的播放头替身。

    模拟虚拟音频已经起播、且播放速度与墙钟一致的真实情形 —— 这是时间轴
    正常播放路径的基准。播放头停住 (_FrozenClock) 用于制造迟到场景。
    """

    def __init__(self) -> None:
        self._t0 = None

    def __call__(self, audio_id: str):
        import time as _time

        if self._t0 is None:
            self._t0 = _time.monotonic()
        played = int((_time.monotonic() - self._t0) * 16000)
        return _clock(played)


class _FrozenClock:
    """播放头固定不前：制造「批处理迟到」场景。"""

    def __init__(self, samples_played: int, *, finished: bool = True) -> None:
        self._samples = samples_played
        self._finished = finished

    def __call__(self, audio_id: str):
        return _clock(self._samples, finished=self._finished)


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, batch_payload=None) -> None:
        self._batch_payload = batch_payload
        self.batch_calls = 0
        self.closed = False

    def post(self, url, **kwargs):
        if url.endswith("/render/batch"):
            self.batch_calls += 1
            return _FakeResponse(self._batch_payload or {"frames": []})
        if url.endswith("/render/cancel"):
            return _FakeResponse({"ok": True})
        raise AssertionError(f"unexpected url {url}")

    def get(self, url, **kwargs):
        return _FakeResponse({"backend_ready": True})

    async def close(self):
        self.closed = True


class _RecordingBus:
    """记录帧总线发布的替身。"""

    def __init__(self) -> None:
        self.calls = []

    def publish_frame(self, frame_rgb, *, owner, priority, frame_index=None,
                      pts_ms=None, compose=True):
        self.calls.append({"owner": owner, "priority": priority,
                           "frame_index": frame_index, "pts_ms": pts_ms})
        return True, frame_rgb

    def encode_jpeg(self, *a, **k):
        return b""

    @property
    def count(self):
        return len(self.calls)


def _provider(batch_payload) -> tuple[LatentSyncBatchAvatarProvider, _FakeSession]:
    provider = LatentSyncBatchAvatarProvider(
        provider_id="latentsync_batch_test",
        display_name="LatentSync Batch Test",
        node_url="http://node.invalid",
    )
    session = _FakeSession(batch_payload)
    provider._session = session  # 绕过真实网络
    provider._is_ready = True
    return provider, session


def _batch_payload(count: int) -> dict:
    jpeg = _jpeg_bytes()
    return {
        "frames": [base64.b64encode(jpeg).decode("ascii")] * count,
        "output_frames": count,
    }


class TestBatchSlicePlayback(unittest.IsolatedAsyncioTestCase):
    """整句渲染产出的帧必须真正按音频播放头上屏。"""

    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _patch_bus(self):
        import server.core.media.frame_bus as frame_bus_mod
        from unittest import mock

        bus = _RecordingBus()
        p = mock.patch.object(frame_bus_mod, "global_frame_bus", bus)
        p.start()
        self._patchers.append(p)
        return bus

    def _patch_clock(self, factory):
        from unittest import mock

        p = mock.patch(
            "server.core.media.virtual_audio.global_virtual_audio.get_playback_clock",
            factory,
        )
        p.start()
        self._patchers.append(p)

    async def test_render_publishes_all_frames_to_frame_bus(self):
        """核心回归：渲染出的每一帧都必须发布到统一帧总线（而非只留最后一帧预览）。"""
        bus = self._patch_bus()
        self._patch_clock(_WallClock())

        provider, _ = _provider(_batch_payload(3))
        result = await provider.render_sentence(
            _audio_frames("aud_pub"), render_mode=AvatarRenderMode.BATCH
        )
        self.assertEqual(result.output_frames, 3)

        for _ in range(300):
            if bus.count >= 3:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(bus.count, 3, "缓存帧必须全部发布到帧总线")
        self.assertEqual({c["owner"] for c in bus.calls}, {"latentsync_batch"})
        # 云端帧必须以高优先级抢占本地 shadow 租约
        self.assertEqual({c["priority"] for c in bus.calls}, {100})

    async def test_publish_false_only_caches_without_publishing(self):
        """预取 (publish=False) 只落缓存，绝不上屏，否则会与正式播放重复发布。"""
        bus = self._patch_bus()

        provider, _ = _provider(_batch_payload(2))
        await provider.render_sentence(
            _audio_frames("aud_cache"), render_mode=AvatarRenderMode.BATCH, publish=False
        )
        await asyncio.sleep(0.05)

        self.assertEqual(bus.count, 0)
        self.assertTrue(provider.has_cached_slice("aud_cache"))

    async def test_cached_slice_replays_without_cloud_call(self):
        """命中预取缓存时直接上屏，不再发起云端批处理请求。"""
        bus = self._patch_bus()
        self._patch_clock(_WallClock())

        provider, session = _provider(_batch_payload(2))
        await provider.render_sentence(
            _audio_frames("aud_replay"), render_mode=AvatarRenderMode.BATCH, publish=False
        )
        self.assertTrue(provider.has_cached_slice("aud_replay"))
        calls_before = session.batch_calls

        played = await provider.play_cached_slice("aud_replay")
        self.assertTrue(played)
        self.assertEqual(session.batch_calls, calls_before, "命中缓存不得再请求云端")

        for _ in range(200):
            if bus.count:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(bus.count, "命中缓存后必须真正上屏")
        self.assertFalse(provider.has_cached_slice("aud_replay"), "播放后缓存应被消费")

    async def test_miss_returns_false(self):
        provider, _ = _provider({"frames": []})
        self.assertFalse(await provider.play_cached_slice("nope"))

    async def test_interrupt_cancels_playback(self):
        """打断必须立即停止上屏，否则旧画面会拖尾。"""
        bus = self._patch_bus()
        # 播放头不推进 -> 时间轴等待，便于在等待期打断
        self._patch_clock(lambda audio_id: _clock(0))

        provider, _ = _provider(_batch_payload(200))
        await provider.render_sentence(
            _audio_frames("aud_int", duration_ms=8000), render_mode=AvatarRenderMode.BATCH
        )
        await asyncio.sleep(0.05)
        self.assertTrue(provider.is_playing, "播放时间轴应处于运行态")

        # 播放头停在 0：第 0 帧 (PTS=0) 本就应该上屏，其后各帧必须挂起等待
        at_interrupt = bus.count
        self.assertLessEqual(at_interrupt, 1)

        await provider.interrupt("test-barge-in")
        await asyncio.sleep(0.15)
        self.assertFalse(provider.is_playing)
        self.assertEqual(bus.count, at_interrupt, "打断后不得继续上屏（旧画面拖尾）")

    async def test_interrupted_clock_stops_playback(self):
        """音频被真实打断时，时间轴必须自行退出（与 sidecar 时间线同语义）。"""
        bus = self._patch_bus()
        self._patch_clock(
            lambda audio_id: {
                "has_started": True,
                "samples_played": 0,
                "elapsed_sec": 0.0,
                "is_interrupted": True,
                "is_rejected": False,
                "is_finished": False,
                "reject_reason": "",
            }
        )
        provider, _ = _provider(_batch_payload(50))
        await provider.render_sentence(
            _audio_frames("aud_stop", duration_ms=2000), render_mode=AvatarRenderMode.BATCH
        )
        for _ in range(200):
            if not provider.is_playing:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(provider.is_playing)
        self.assertEqual(bus.count, 0)

    async def test_browser_fallback_reasons_do_not_stop_playback(self):
        """浏览器兜底播放（无虚拟音频）时必须改用墙钟继续上屏，不能误停。"""
        bus = self._patch_bus()
        self._patch_clock(
            lambda audio_id: {
                "has_started": False,
                "samples_played": 0,
                "elapsed_sec": 0.0,
                "is_rejected": True,
                "is_interrupted": False,
                "is_finished": False,
                "reject_reason": "service_disabled",
            }
        )
        provider, _ = _provider(_batch_payload(3))
        await provider.render_sentence(
            _audio_frames("aud_fb"), render_mode=AvatarRenderMode.BATCH
        )
        for _ in range(300):
            if bus.count >= 3:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(bus.count, 3)
        self.assertIn("service_disabled", PLAYBACK_FALLBACK_REASONS)

    async def test_late_arrival_is_reported_honestly(self):
        """音频已播过整句才拿到帧时，必须如实上报迟到与丢帧，而不是假装同步。"""
        bus = self._patch_bus()
        self._patch_clock(lambda audio_id: _clock(16000 * 5, finished=True))

        provider, _ = _provider(_batch_payload(10))
        await provider.render_sentence(
            _audio_frames("aud_late", duration_ms=400), render_mode=AvatarRenderMode.BATCH
        )
        for _ in range(300):
            if not provider.is_playing:
                break
            await asyncio.sleep(0.01)

        status = provider.get_preview_status()
        self.assertGreaterEqual(status["late_arrival_count"], 1)
        self.assertGreater(status["dropped_frames"], 0)
        # 迟到时不得抢占本地 shadow：宁可留在 shadow 也不播出错的嘴型
        self.assertEqual(bus.count, 0)

    async def test_frames_rejected_when_not_structurally_complete_jpeg(self):
        """结构非法的 JPEG 不得上屏（避免把损坏数据推给平台）。"""
        bus = self._patch_bus()
        self._patch_clock(_WallClock())

        bad = base64.b64encode(b"not-a-jpeg").decode("ascii")
        provider, _ = _provider({"frames": [bad, bad], "output_frames": 2})
        await provider.render_sentence(
            _audio_frames("aud_bad"), render_mode=AvatarRenderMode.BATCH
        )
        for _ in range(200):
            if not provider.is_playing:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(bus.count, 0)
        self.assertGreaterEqual(provider.dropped_frames, 1)

    async def test_status_reports_playback_progress(self):
        self._patch_bus()
        self._patch_clock(_WallClock())
        provider, _ = _provider(_batch_payload(4))
        await provider.render_sentence(
            _audio_frames("aud_stat"), render_mode=AvatarRenderMode.BATCH
        )
        for _ in range(300):
            if provider.published_frames >= 4:
                break
            await asyncio.sleep(0.01)
        status = provider.get_preview_status()
        self.assertEqual(status["published_frames"], 4)
        self.assertEqual(status["dropped_frames"], 0)
        self.assertEqual(status["late_arrival_count"], 0)

    async def test_capabilities_declare_batch_playback(self):
        provider, _ = _provider({"frames": []})
        caps = provider.get_media_capabilities()
        self.assertTrue(caps["batch_slice_playback"])
        self.assertTrue(caps["slice_prefetch"])


class TestPrefetchIntegration(unittest.IsolatedAsyncioTestCase):
    """SlicePrefetcher 必须驱动 provider 的 publish=False 预取通道。"""

    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _patch_bus(self):
        import server.core.media.frame_bus as frame_bus_mod
        from unittest import mock

        bus = _RecordingBus()
        p = mock.patch.object(frame_bus_mod, "global_frame_bus", bus)
        p.start()
        self._patchers.append(p)
        return bus

    async def test_prefetcher_prefetches_without_publishing(self):
        bus = self._patch_bus()
        provider, _ = _provider(_batch_payload(3))

        from server.core.avatar.slice_prefetcher import SlicePrefetcher

        prefetcher = SlicePrefetcher(provider, lookahead_depth=2)
        await prefetcher.prefetch_sentence(
            "aud_pf", _audio_frames("aud_pf"), "预取测试"
        )
        for _ in range(300):
            if provider.has_cached_slice("aud_pf"):
                break
            await asyncio.sleep(0.01)

        self.assertTrue(provider.has_cached_slice("aud_pf"))
        self.assertEqual(bus.count, 0, "预取阶段不得上屏")
        self.assertEqual(prefetcher.ready_count, 1)

        acquired = await prefetcher.acquire_slice("aud_pf")
        self.assertIsNotNone(acquired)
        self.assertEqual(acquired.state.value, "consumed")

    async def test_prefetcher_acquire_returns_playable_slice(self):
        """预取命中的切片必须能直接驱动播放，而不只是拿到一个对象。"""
        bus = self._patch_bus()
        from unittest import mock

        p = mock.patch(
            "server.core.media.virtual_audio.global_virtual_audio.get_playback_clock",
            _WallClock(),
        )
        p.start()
        self._patchers.append(p)

        provider, _ = _provider(_batch_payload(3))
        from server.core.avatar.slice_prefetcher import SlicePrefetcher

        prefetcher = SlicePrefetcher(provider, lookahead_depth=2)
        await prefetcher.prefetch_sentence(
            "aud_play", _audio_frames("aud_play"), "命中播放"
        )
        for _ in range(300):
            if prefetcher.ready_count:
                break
            await asyncio.sleep(0.01)

        acquired = await prefetcher.acquire_slice("aud_play")
        self.assertIsNotNone(acquired)
        self.assertTrue(acquired.slice_data)
        for _ in range(300):
            if bus.count >= 3:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(bus.count, 3)

    async def test_interrupt_clears_prefetch_and_cache(self):
        bus = self._patch_bus()
        provider, _ = _provider(_batch_payload(2))

        from server.core.avatar.slice_prefetcher import SlicePrefetcher

        prefetcher = SlicePrefetcher(provider, lookahead_depth=2)
        await prefetcher.prefetch_sentence(
            "aud_clr", _audio_frames("aud_clr"), "清理测试"
        )
        for _ in range(300):
            if provider.has_cached_slice("aud_clr"):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(provider.has_cached_slice("aud_clr"))

        await prefetcher.interrupt_and_clear()
        self.assertFalse(provider.has_cached_slice("aud_clr"))
        self.assertEqual(prefetcher.ready_count, 0)
        self.assertEqual(bus.count, 0)


if __name__ == "__main__":
    unittest.main()
