"""
单元测试：内置 RTMP 直推引擎 (RtmpStreamerService)
"""

import numpy as np
import pytest
from server.core.media.rtmp_streamer import RtmpStreamerService, find_ffmpeg_binary, global_rtmp_streamer


def test_rtmp_streamer_initial_state():
    streamer = RtmpStreamerService()
    status = streamer.get_status()
    assert status["is_streaming"] is False
    assert status["state"] == "stopped"
    assert status["frames_sent"] == 0
    assert status["audio_bytes_sent"] == 0


def test_rtmp_streamer_masking():
    streamer = RtmpStreamerService()
    assert streamer._mask_stream_url("", "") == ""
    assert streamer._mask_stream_url("rtmp://example.com/live", "") == "rtmp://example.com/live"
    masked = streamer._mask_stream_url("rtmp://example.com/live", "live_stream_key_123456")
    assert "live_stream_key" not in masked
    assert "****" in masked


def test_rtmp_streamer_validation_on_empty_url():
    streamer = RtmpStreamerService()
    success, msg = streamer.start("")
    assert success is False
    assert "推流地址不能为空" in msg
    assert streamer.get_status()["state"] == "error"


def test_rtmp_streamer_safe_writes_when_stopped():
    streamer = RtmpStreamerService()
    # 在未启动状态下尝试写入，必须静默返回 False，绝不能抛出异常
    assert streamer.send_video_frame(b"\x00" * 100) is False
    assert streamer.send_audio_pcm(b"\x00" * 100) is False


def test_find_ffmpeg_binary():
    # 验证系统环境能成功嗅探到 ffmpeg
    bin_path = find_ffmpeg_binary()
    assert bin_path is not None
    assert "ffmpeg" in bin_path.lower()


def test_rtmp_streamer_stop_when_stopped():
    streamer = RtmpStreamerService()
    assert streamer.stop() is True
    status = streamer.get_status()
    assert status["is_streaming"] is False
    assert status["state"] == "stopped"


def test_rtmp_streamer_send_invalid_data():
    streamer = RtmpStreamerService()
    # 非法数据输入均安全返回 False，不崩溃
    assert streamer.send_video_frame(12345) is False
    assert streamer.send_video_frame(None) is False
    assert streamer.send_audio_pcm(None) is False
    assert streamer.send_audio_pcm(b"") is False


# ---------------------------------------------------------------------------
# P1-3: H.264 profile 钉死与断线重连看门狗
# ---------------------------------------------------------------------------
def test_h264_profile_is_pinned_in_command(monkeypatch):
    """FFmpeg 命令行必须显式钉死 H.264 profile，兼容主流 CDN"""
    streamer = RtmpStreamerService()
    streamer.rtmp_url = "rtmp://example.com/live"
    streamer.stream_key = "key123"
    streamer._audio_port = 18000  # noqa: SLF001 - 仅构造命令，不真正绑定
    monkeypatch.setattr(
        "server.core.media.rtmp_streamer.find_ffmpeg_binary",
        lambda: "/usr/bin/ffmpeg",
    )
    cmd = streamer._build_ffmpeg_cmd("rtmp://example.com/live/key123")  # noqa: SLF001
    assert "-profile:v" in cmd
    idx = cmd.index("-profile:v")
    assert cmd[idx + 1] == streamer.h264_profile
    # GOP 仍为 2 秒关键帧间隔
    assert "-g" in cmd
    assert int(cmd[cmd.index("-g") + 1]) == streamer.fps * 2


def test_reconnect_loop_respects_stop_event(monkeypatch):
    """stop() 置位后重连看门狗必须立即退出，不得重新拉起推流"""
    streamer = RtmpStreamerService()
    streamer.rtmp_url = "rtmp://example.com/live"
    streamer.stream_key = "key123"
    streamer._stop_event.set()  # noqa: SLF001

    spawn_calls = []

    def fake_spawn():
        spawn_calls.append(1)
        return True, "ok"

    monkeypatch.setattr(streamer, "_spawn_process", fake_spawn)
    monkeypatch.setattr(streamer, "_teardown_process", lambda: None)
    # 退避时间压到极小，让循环快速推进
    monkeypatch.setattr(streamer, "_reconnect_base_delay", 0.01)
    monkeypatch.setattr(streamer, "_reconnect_max_delay", 0.02)

    streamer._reconnect_loop()  # noqa: SLF001
    # stop 已置位，看门狗不应尝试任何重连
    assert spawn_calls == []


def test_reconnect_loop_gives_up_after_max_attempts(monkeypatch):
    """达到最大重连次数后停止自愈并记录错误"""
    streamer = RtmpStreamerService()
    streamer.rtmp_url = "rtmp://example.com/live"
    streamer.stream_key = "key123"
    monkeypatch.setattr(streamer, "_teardown_process", lambda: None)
    monkeypatch.setattr(streamer, "_spawn_process", lambda: (False, "boom"))
    monkeypatch.setattr(streamer, "_reconnect_base_delay", 0.001)
    monkeypatch.setattr(streamer, "_reconnect_max_delay", 0.002)

    streamer._reconnect_loop()  # noqa: SLF001
    status = streamer.get_status()
    assert status["reconnect_attempts"] == streamer._max_reconnect_attempts  # noqa: SLF001
    assert status["state"] == "error"
    assert "boom" in status["last_reconnect_error"]


def test_reconnect_loop_recovers_and_counts_success(monkeypatch):
    """重连成功后计数 +1 且状态恢复"""
    streamer = RtmpStreamerService()
    streamer.rtmp_url = "rtmp://example.com/live"
    streamer.stream_key = "key123"
    results = [(False, "fail"), (True, "ok")]

    def fake_spawn():
        return results.pop(0)

    monkeypatch.setattr(streamer, "_teardown_process", lambda: None)
    monkeypatch.setattr(streamer, "_spawn_process", fake_spawn)
    monkeypatch.setattr(streamer, "_reconnect_base_delay", 0.001)
    monkeypatch.setattr(streamer, "_reconnect_max_delay", 0.002)

    streamer._reconnect_loop()  # noqa: SLF001
    status = streamer.get_status()
    assert status["reconnect_success_total"] == 1
    assert status["reconnect_attempts"] == 2


def test_schedule_reconnect_does_not_stack_watchdogs(monkeypatch):
    """重复触发只保留一个待执行看门狗"""
    streamer = RtmpStreamerService()
    streamer.rtmp_url = "rtmp://example.com/live"
    streamer.stream_key = "key123"

    class FakeThread:
        def __init__(self, target, **kwargs):
            self.is_alive = lambda: True

        def start(self):
            pass

    monkeypatch.setattr("server.core.media.rtmp_streamer.threading.Thread", FakeThread)
    streamer._schedule_reconnect()  # noqa: SLF001
    first = streamer._reconnect_thread  # noqa: SLF001
    streamer._schedule_reconnect()  # noqa: SLF001
    # 重复触发复用同一个看门狗，不堆叠
    assert streamer._reconnect_thread is first  # noqa: SLF001


def test_schedule_reconnect_respects_stop(monkeypatch):
    """stop() 后触发管道错误不再安排重连"""
    streamer = RtmpStreamerService()
    streamer._stop_event.set()  # noqa: SLF001
    streamer._schedule_reconnect()  # noqa: SLF001
    assert streamer._reconnect_thread is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# P1-4: 音频采样率统一 (RTMP 侧重采样)
# ---------------------------------------------------------------------------
def test_resample_s16le_same_rate_passthrough():
    from server.core.media.rtmp_streamer import _resample_s16le

    data = b"\x00\x01" * 100
    assert _resample_s16le(data, 24000, 24000) == data


def test_resample_s16le_halves_sample_count():
    from server.core.media.rtmp_streamer import _resample_s16le

    # 48k -> 24k：样本数应减半，时长不变
    data = (np.arange(480, dtype=np.int16)).tobytes()
    out = _resample_s16le(data, 48000, 24000)
    samples = np.frombuffer(out, dtype=np.int16)
    assert samples.size == 240


def test_resample_s16le_doubles_sample_count():
    from server.core.media.rtmp_streamer import _resample_s16le

    data = (np.arange(240, dtype=np.int16)).tobytes()
    out = _resample_s16le(data, 24000, 48000)
    samples = np.frombuffer(out, dtype=np.int16)
    assert samples.size == 480


def test_resample_s16le_handles_empty_and_garbage():
    from server.core.media.rtmp_streamer import _resample_s16le

    assert _resample_s16le(b"", 24000, 48000) == b""
    # 非法采样率原样返回，不抛错
    assert _resample_s16le(b"\x00\x01", 0, 48000) == b"\x00\x01"


