"""
单元测试：内置 RTMP 直推引擎 (RtmpStreamerService)
"""

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

