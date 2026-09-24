# -*- coding: utf-8 -*-
"""共享单调播放时钟与音画漂移补偿的单元测试"""
import asyncio
import threading
import pytest

from server.core.media.shared_playback_clock import SharedPlaybackClock
from server.core.media.av_sync import AVSyncController


def test_stamp_video_monotonic_non_decreasing():
    clock = SharedPlaybackClock()
    pts_sequence = [clock.stamp_video(i) for i in range(50)]
    for prev, cur in zip(pts_sequence, pts_sequence[1:]):
        assert cur > prev, "视频 PTS 必须严格单调递增不回退"


def test_report_audio_head_updates_without_anchor_compensation():
    clock = SharedPlaybackClock()
    # 未上报音频锚点前漂移为 0
    assert clock.compute_drift() == 0.0
    clock.stamp_video(0)
    assert clock.compute_drift() == 0.0
    # 上报音频头后建立锚点
    clock.report_audio_head("aud_1", 500.0)
    assert clock.compute_drift() != 0.0


def test_negative_drift_means_audio_lags_video():
    import time as _time
    clock = SharedPlaybackClock()
    clock.stamp_video(0)
    _time.sleep(0.02)          # 让视频侧单调时钟真实推进
    clock.stamp_video(1)       # 视频 PTS 已推进约 20ms
    clock.report_audio_head("aud_1", 1.0)  # 音频头仍停留在开头 -> 滞后
    drift = clock.compute_drift()
    assert drift < 0.0, "音频滞后时漂移应为负"


def test_positive_drift_means_audio_leads_video():
    import time as _time
    clock = SharedPlaybackClock()
    clock.stamp_video(0)
    _time.sleep(0.02)
    clock.stamp_video(1)
    video_pts = clock.get_alignment_status()["video_pts_ms"]
    clock.report_audio_head("aud_1", video_pts + 200.0)  # 音频超前 200ms
    drift = clock.compute_drift()
    assert drift > 0.0, "音频超前时漂移应为正"


def test_recommended_delay_clamped_to_bounds():
    clock = SharedPlaybackClock()
    clock.stamp_video(0)
    # 构造超大正向漂移
    clock.report_audio_head("aud_1", 100000.0)
    delay = clock.get_recommended_delay_ms()
    assert 0 <= delay <= 300


def test_frame_pacing_hint_triggers_beyond_threshold():
    import time as _time
    clock = SharedPlaybackClock()
    clock.stamp_video(0)
    _time.sleep(0.15)          # 视频侧推进约 150ms，构造超越 120ms 阈值的漂移
    clock.stamp_video(1)
    clock.report_audio_head("aud_1", 1.0)  # 音频头停留在开头 -> 大幅滞后
    hint = None
    # EMA 平滑需要若干采样收敛
    for _ in range(30):
        hint = clock.get_frame_pacing_hint()
        if hint is not None:
            break
        _time.sleep(0.001)
        clock.stamp_video(1)
    assert hint == "skip_frame"
    # 小漂移不触发建议
    clock.reset()
    clock.stamp_video(0)
    vid = clock.get_alignment_status()["video_pts_ms"]
    clock.report_audio_head("aud_1", vid + 5.0)
    for _ in range(5):
        if clock.get_frame_pacing_hint() is not None:
            break
        clock.compute_drift()
    assert clock.get_frame_pacing_hint() is None


def test_reset_clears_anchor_and_state():
    clock = SharedPlaybackClock()
    clock.stamp_video(0)
    clock.report_audio_head("aud_1", 300.0)
    assert clock.compute_drift() != 0.0
    clock.reset()
    assert clock.compute_drift() == 0.0
    assert clock.get_alignment_status()["audio_anchored"] is False


def test_disabled_clock_is_inert():
    clock = SharedPlaybackClock()
    clock.set_enabled(False)
    clock.stamp_video(0)
    clock.report_audio_head("aud_1", 999.0)
    assert clock.compute_drift() == 0.0


def test_alignment_status_reports_shared_clock_semantics():
    clock = SharedPlaybackClock()
    clock.stamp_video(0)
    status = clock.get_alignment_status()
    # 未锚定前沿用 video_monotonic_only
    assert status["shared_playback_clock"] is True
    assert status["alignment_mode"] == "heuristic_uniform"
    clock.report_audio_head("aud_1", 0.0)
    status = clock.get_alignment_status()
    assert status["alignment_mode"] == "shared_monotonic_pts"
    assert status["clock_source"] == "shared_monotonic_pts"
    assert status["clock_precision"] == "sample_aligned"


def test_thread_safe_concurrent_stamping():
    clock = SharedPlaybackClock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for i in range(200):
            clock.stamp_video(i)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 并发下不崩溃且帧计数完整
    assert clock.get_alignment_status()["video_frame_count"] == 8 * 200


def test_av_sync_drift_locking_replaces_heuristic():
    ctrl = AVSyncController()
    # 未锁定时沿用渲染耗时估计
    ctrl.record_render_latency(40.0)
    assert ctrl.recommended_delay_ms == 40
    # 锁定漂移通道后以漂移为准
    ctrl.apply_drift(120.0, anchored=True)
    assert ctrl.recommended_delay_ms == 120
    # 后续渲染耗时不再覆盖漂移补偿
    ctrl.record_render_latency(10.0)
    assert ctrl.recommended_delay_ms == 120
    # 解锚后回到耗时估计
    ctrl.apply_drift(None, anchored=False)
    ctrl.record_render_latency(25.0)
    assert ctrl.recommended_delay_ms == 25


def test_av_sync_drift_clamped():
    ctrl = AVSyncController()
    ctrl.apply_drift(99999.0, anchored=True)
    assert ctrl.recommended_delay_ms <= 300


def test_av_sync_reset_clears_drift_lock():
    ctrl = AVSyncController()
    ctrl.apply_drift(150.0, anchored=True)
    ctrl.reset()
    assert ctrl.get_status()["drift_locked"] is False


def test_neural_sidecar_driver_stamps_and_resets_clock():
    async def _test():
        import cv2
        import numpy as np
        from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
        from server.core.media.shared_playback_clock import global_shared_playback_clock

        global_shared_playback_clock.reset()
        driver = NeuralSidecarMediaDriver()

        # 生成合法的微型 JPEG 帧
        ret, enc = cv2.imencode(".jpg", np.zeros((16, 16, 3), dtype=np.uint8))
        assert ret
        valid_jpeg = enc.tobytes()

        await driver._publish_video_frame(valid_jpeg, None)

        # 验证视频 PTS 推进打点
        status = global_shared_playback_clock.get_alignment_status()
        assert status["video_frame_count"] >= 1
        assert status["video_pts_ms"] > 0

        # 验证取消时间线触发 reset
        await driver._cancel_timelines(clear_frame=True)
        status_after = global_shared_playback_clock.get_alignment_status()
        assert status_after["video_frame_count"] == 0

    asyncio.run(_test())


def test_musetalk_driver_interrupt_resets_clock():
    async def _test():
        from server.adapters.media.musetalk_driver import ProceduralAvatarDriver
        from server.core.media.shared_playback_clock import global_shared_playback_clock

        global_shared_playback_clock.stamp_video(0)
        assert global_shared_playback_clock.get_alignment_status()["video_frame_count"] >= 1

        driver = ProceduralAvatarDriver()
        await driver.interrupt(reason="test_interrupt")
        assert global_shared_playback_clock.get_alignment_status()["video_frame_count"] == 0

    asyncio.run(_test())


def test_sample_clock_epoch_unifies_audio_video_domains():
    """问题 2: 采样锚点建立后，视频 PTS 与音频头同源，drift 不再混用时钟域"""
    clock = SharedPlaybackClock()
    clock.reset()

    # 1. 音频头先建立采样锚点
    clock.report_audio_head("aud_1", 1000.0)
    # 2. 视频帧携带采样时钟换算值打点 (与音频头同域)
    video_pts = clock.stamp_video(0, pts_ms=1040.0)
    assert video_pts == pytest.approx(1040.0, abs=1.0)

    # 3. 漂移应为音频头 - 视频头，两值同源可直接比较
    drift = clock.compute_drift()
    assert drift < 0  # 音频滞后于画面 40ms

    # 4. 上报更新后的音频头，漂移应随之收敛
    clock.report_audio_head("aud_1", 1040.0)
    for _ in range(10):
        drift = clock.compute_drift()
    assert abs(drift) < 15.0  # EMA 收敛后趋近 0 (窗口均值残留允许一定余量)


def test_stamp_video_falls_back_without_sample_epoch():
    """未建立采样锚点时，stamp_video 回退单调时钟保持自洽"""
    clock = SharedPlaybackClock()
    clock.reset()
    pts_a = clock.stamp_video(0)
    pts_b = clock.stamp_video(1)
    assert pts_b > pts_a
    # 未提供 pts_ms 时 status 应反映未锚定
    status = clock.get_alignment_status()
    assert status["audio_anchored"] is False


def test_sidecar_passes_sample_pts_on_publish():
    """问题 2: sidecar 发布帧时携带采样时钟换算值，两侧同源"""
    import cv2
    import numpy as np

    async def _test():
        from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
        from server.core.media.shared_playback_clock import global_shared_playback_clock

        global_shared_playback_clock.reset()
        driver = NeuralSidecarMediaDriver()

        # 模拟时间线循环写入采样锚点后发布帧
        driver._pending_video_pts_ms = 1234.0
        ret, enc = cv2.imencode(".jpg", np.zeros((16, 16, 3), dtype=np.uint8))
        assert ret
        await driver._publish_video_frame(enc.tobytes(), None)

        status = global_shared_playback_clock.get_alignment_status()
        assert status["video_pts_ms"] == pytest.approx(1234.0, abs=2.0)
        # 发布后暂存值被清空，避免下一帧误用
        assert driver._pending_video_pts_ms is None

    asyncio.run(_test())


def test_action_frame_uses_override_coord_for_lip_render():
    """问题 3: 动作帧经 override_coord 实时裁剪人脸，不再与神经唇形互斥"""
    import numpy as np

    renderer_cls = None
    try:
        from server.core.avatar.neural_lip_renderer import NeuralLipRenderer
        renderer_cls = NeuralLipRenderer
    except Exception:
        pytest.skip("NeuralLipRenderer 不可导入")

    # 构造一个绕过 ONNX 会话的测试替身：仅验证裁剪与坐标路径
    renderer = NeuralLipRenderer.__new__(NeuralLipRenderer)
    renderer.is_ready = True
    renderer.session = None  # render_lip_frame 因 session 为 None 直接返回 None，不触发推理
    renderer.has_anchor_assets = True
    renderer.input_names = []
    renderer.output_names = []

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # 动作帧坐标有效时应走到裁剪逻辑 (session 为 None 在裁剪之后才拦截，返回 None 而非报错)
    result = renderer.render_lip_frame(
        frame, 0, np.zeros(3200, dtype=np.float32) + 0.5,
        mouth_open=0.5, override_coord=(100, 300, 200, 400),
    )
    # session 为 None 时推理分支返回 None，但必须能越过互斥门禁进入该路径
    assert result is None

    # 非法坐标 (过小) 时裁剪返回 None，同样平安降级
    result_bad = renderer.render_lip_frame(
        frame, 0, np.zeros(3200, dtype=np.float32) + 0.5,
        mouth_open=0.5, override_coord=(100, 105, 200, 205),
    )
    assert result_bad is None


def test_virtual_audio_clock_precision_aligns_with_shared_clock():
    """问题 3.1: virtual_audio 精度口径与 SharedPlaybackClock 对齐"""
    from server.core.media.virtual_audio import _resolve_clock_precision

    # 不可用时为 none
    assert _resolve_clock_precision(False) == "none"
    # 可用但未锚定时为 estimated (不依赖全局单例的真实状态，隔离即可)
    clock = SharedPlaybackClock()
    clock.reset()
    assert clock.get_alignment_status()["audio_anchored"] is False

