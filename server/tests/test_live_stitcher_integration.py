# -*- coding: utf-8 -*-
"""
验证缺口 A/B 与 P0 音频链路：
1. HybridStitcher 接入直播主链路全生命周期 (切片装载、打断停顿、实时动轨、平滑回切与大屏监控)
2. 静轨切片音频真实投递至 global_virtual_audio (支持剥离WAV头、动态微变速、打断代际栅栏与断点恢复)
3. 过场词接通 TTS 合成并送入 virtual_audio 播放
"""
import io
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from server.core.media.hybrid_stitcher import HybridSliceHandle
from server.core.media.stitcher_protocol import StitcherState
from server.core.media.virtual_audio import global_virtual_audio
from server.routes.live import global_live_controller


def _generate_test_wav(filepath: Path, duration_sec: float = 1.0, sample_rate: int = 16000) -> bytes:
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    pcm16 = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767.0).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    wav_bytes = buf.getvalue()
    filepath.write_bytes(wav_bytes)
    return wav_bytes


@pytest.mark.anyio
async def test_live_stitcher_integration_lifecycle(tmp_path: Path):
    ctrl = global_live_controller
    assert ctrl.stitcher is not None

    # 1. 模拟开播环境与可用切片入队 (真实生成 1 秒标准 WAV 音频文件)
    ctrl.is_live = True
    ctrl.session_id = "test_live_session_999"

    fake_video = tmp_path / "slice.mp4"
    fake_video.write_bytes(b"dummy_mp4_bytes")
    fake_audio = tmp_path / "audio.wav"
    _generate_test_wav(fake_audio, duration_sec=1.0)

    slice_handle = HybridSliceHandle(
        slice_id="slice_prod1_intro_001",
        product_id="prod_test_1",
        slot_code="intro",
        video_path=str(fake_video),
        audio_path=str(fake_audio),
        duration_sec=1.0,
        loudness_lufs=-16.0,
        noise_profile={"noise_floor_db": -52.0},
    )
    ctrl.stitcher.clear_playlist()
    ctrl.stitcher.enqueue_slice(slice_handle)

    # 验证在播切片已进入主调度器
    assert ctrl.stitcher.current_slice is not None
    assert ctrl.stitcher.current_slice.slice_id == "slice_prod1_intro_001"
    assert ctrl.stitcher.state == StitcherState.PLAYING_BATCH_SLICE

    # 验证静轨切片音频真实播放链路：首帧驱动时切片音频必须投递给 global_virtual_audio
    with patch.object(global_virtual_audio, "play_chunk", return_value=True) as mock_play:
        ctrl.stitcher.tick_frame()
        assert mock_play.called is True
        call_args = mock_play.call_args
        played_bytes = call_args[0][0]
        # 断言投递的音频字节数有效，且不是带 RIFF 头的未剥离文件
        assert len(played_bytes) > 0
        assert not played_bytes.startswith(b"RIFF")
        assert call_args[1].get("codec") == "pcm_s16le"
        assert call_args[1].get("allow_raw_pcm") is True
        assert "slice_prod1_intro_001" in call_args[1].get("audio_id", "")

    # 2. 模拟触发观众弹幕打断 (barge-in)
    with patch.object(global_virtual_audio, "stop") as mock_stop:
        await ctrl._on_barge_in("观众提问尺码")
        # 必须通知 virtual_audio 停止当前音频，清空播放队列
        assert mock_stop.called is True

    # 状态机必须跃迁进入打断停顿态
    assert ctrl.stitcher.state == StitcherState.INTERRUPTED_HOLDING

    # 模拟 4 步缓动停顿消耗完成，转入实时动轨驱动状态
    for _ in range(4):
        ctrl.stitcher.tick_frame()
    assert ctrl.stitcher.state == StitcherState.STREAMING_REALTIME

    # 3. 实时动轨供帧
    ctrl.stitcher.feed_realtime_frame(None)

    # 4. 模拟弹幕播报回复完毕，平滑回切 (验证断点音频恢复投递)
    resumed = ctrl.stitcher.finish_realtime_reply()
    assert resumed is True
    assert ctrl.stitcher.state == StitcherState.BLENDING_TRANSITION

    with patch.object(global_virtual_audio, "play_chunk", return_value=True) as mock_resume_play:
        # 模拟 4 步过渡缓动完成，恢复播放切片
        for _ in range(4):
            ctrl.stitcher.tick_frame()
        assert ctrl.stitcher.state == StitcherState.PLAYING_BATCH_SLICE
        # 验证缓动结束回到切片断点时，切片音频断点恢复重新被投递
        assert mock_resume_play.called is True

    # 5. 验证过场词接通 TTS 合成并向 virtual_audio 投递
    with patch("server.core.audio.tts_preview_service.synthesize_preview_audio", new=AsyncMock(return_value=(b"fake_filler_wav", "audio/wav"))), \
         patch.object(global_virtual_audio, "play_chunk", return_value=True) as mock_filler_play:
        ctrl._on_slice_transition_filler(slice_handle, slice_handle, "好的家人们咱们接着看")
        # 允许事件循环微任务执行
        import asyncio
        await asyncio.sleep(0.05)
        assert mock_filler_play.called is True
        filler_call = mock_filler_play.call_args
        assert filler_call[0][0] == b"fake_filler_wav"
        assert "filler_" in filler_call[1].get("audio_id", "")

    # 6. 模拟停播与资源清理
    await ctrl.stop()
    assert ctrl.is_live is False
    assert ctrl.stitcher.current_slice is None

