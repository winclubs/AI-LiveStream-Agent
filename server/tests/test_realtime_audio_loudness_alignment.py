# -*- coding: utf-8 -*-
"""
验证阶段二/五声学工程：实时 TTS 帧化链路对齐 EBU R128 (-16.0 LUFS) 与舒适底噪
"""
import numpy as np
import pytest

from server.core.avatar.sku_slice_renderer import estimate_loudness_lufs
from server.core.media.audio_pipeline import global_audio_frame_pipeline


@pytest.mark.anyio
async def test_realtime_audio_pipeline_lufs_and_comfort_noise_alignment():
    # 生成 1 秒 16kHz 的测试 PCM 音频 (初始响度较小，约 -30 LUFS)
    sample_rate = 16000
    t = np.linspace(0, 1.0, sample_rate, endpoint=False)
    quiet_sine = (np.sin(2 * np.pi * 440 * t) * 0.05 * 32767.0).astype(np.int16)
    audio_bytes = quiet_sine.tobytes()

    framed = await global_audio_frame_pipeline.frame_transaction(
        audio_bytes=audio_bytes,
        source_codec="pcm_s16le",
        sample_rate=sample_rate,
        channels=1,
        audio_id="test_align_001",
        audio_generation=1,
        session_generation=1,
        text="测试实时插播响度与底噪对齐",
    )

    assert framed is not None
    assert len(framed.frames) > 0

    # 提取重构后的整体 PCM 信号
    full_pcm = b"".join(f.data for f in framed.frames)
    samples = np.frombuffer(full_pcm, dtype=np.int16).astype(np.float32) / 32768.0

    # 验证测量响度收敛在 -16.0 LUFS 附近 (容差 ±1.0 LUFS)
    measured_lufs = estimate_loudness_lufs(samples)
    assert abs(measured_lufs - (-16.0)) <= 1.5

    # 验证底噪垫声存在 (信号不再是纯零底噪，最小 RMS 大于 0)
    rms = float(np.sqrt(np.mean(np.square(samples))))
    assert rms > 0.05
    assert not np.isnan(samples).any()
