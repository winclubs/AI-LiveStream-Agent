# -*- coding: utf-8 -*-
"""共享单调播放时钟与音画漂移补偿的单元测试"""
import threading

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
