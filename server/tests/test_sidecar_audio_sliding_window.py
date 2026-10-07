# -*- coding: utf-8 -*-
"""
针对 Google Colab 云端 GPU 渲染节点 (Sidecar v3) 音频切片与 Mel 提取的回归测试
验证点：
1. 旧逻辑：孤立 640 点补零导致中心 16 步 Mel 频谱矩阵全零 (std=0.0)；
2. 新逻辑：累积音频 + 绝对中心点滑动窗口 (前后 3200 采样点上下文)，Mel std 恢复正常 (>0.7)；
3. 帧数完整对齐：总渲染帧数与音频持续时间严格匹配，无丢帧。
"""
import numpy as np
from server.core.avatar.neural_lip_renderer import MelFeatureExtractor


def test_legacy_cloud_slice_produces_zero_mel():
    """验证旧切片逻辑确实会产生 std=0 的全零纯静音 Mel"""
    PTS_STEP_SAMPLES = 640
    MEL_CONTEXT_SAMPLES = 3200
    extractor = MelFeatureExtractor()

    # 构造一段响亮的 440Hz 正弦波音频 (640 采样点)
    t = np.linspace(0, 640 / 16000.0, 640, endpoint=False)
    pcm_float = (0.8 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    # 旧逻辑：孤立 640 点切片并补零到 6400
    center = len(pcm_float) // 2
    window = pcm_float[max(0, center - MEL_CONTEXT_SAMPLES): center + MEL_CONTEXT_SAMPLES]
    if len(window) < MEL_CONTEXT_SAMPLES * 2:
        window = np.pad(window, (0, MEL_CONTEXT_SAMPLES * 2 - len(window)), mode="constant")

    mel = extractor.extract_mel_window(window, target_steps=16)
    # 铁证：全零补零使得中心截取落入全零区，标准差必为 0
    assert float(mel.std()) == 0.0
    assert np.allclose(mel, np.log(1e-5), atol=1e-3)


def test_sliding_window_produces_rich_mel_and_exact_frames():
    """验证修复后的滑动窗口逻辑：Mel 特征饱满 (std > 0.7) 且总帧数完全对齐"""
    PTS_STEP_SAMPLES = 640
    MEL_CONTEXT_SAMPLES = 3200
    extractor = MelFeatureExtractor()

    # 模拟 2 秒多频段合成语音 (50 视频帧 = 32000 采样点)
    total_frames = 50
    total_samples = total_frames * PTS_STEP_SAMPLES
    t = np.linspace(0, total_samples / 16000.0, total_samples, endpoint=False)
    mock_audio = (
        0.5 * np.sin(2 * np.pi * 300 * t) +
        0.3 * np.sin(2 * np.pi * 1200 * t) +
        0.2 * np.sin(2 * np.pi * 2400 * t)
    ).astype(np.float32)

    # 模拟分块到达 (每块 400ms = 6400 采样点)
    chunk_size = 6400
    chunks = [mock_audio[i: i + chunk_size] for i in range(0, total_samples, chunk_size)]

    audio_pcm_buf = []
    seq = 0
    stds = []

    for ch in chunks:
        audio_pcm_buf.append(ch)
        audio_pcm_all = np.concatenate(audio_pcm_buf) if len(audio_pcm_buf) > 1 else audio_pcm_buf[0]
        audio_pcm_buf = [audio_pcm_all]

        while True:
            center = int(seq * PTS_STEP_SAMPLES + PTS_STEP_SAMPLES // 2)
            if len(audio_pcm_all) < center + MEL_CONTEXT_SAMPLES:
                break
            start = center - MEL_CONTEXT_SAMPLES
            end = center + MEL_CONTEXT_SAMPLES
            pad_left = max(0, -start)
            window = audio_pcm_all[max(0, start): end]
            if pad_left > 0:
                window = np.pad(window, (pad_left, 0), mode="constant")
            mel = extractor.extract_mel_window(window, target_steps=16)
            stds.append(float(mel.std()))
            seq += 1

    # 模拟 render_finish 补齐剩余尾帧
    audio_pcm_all = audio_pcm_buf[0] if audio_pcm_buf else np.zeros(0, dtype=np.float32)
    while seq < total_frames:
        center = int(seq * PTS_STEP_SAMPLES + PTS_STEP_SAMPLES // 2)
        start = center - MEL_CONTEXT_SAMPLES
        end = center + MEL_CONTEXT_SAMPLES
        pad_left = max(0, -start)
        pad_right = max(0, end - len(audio_pcm_all))
        window = audio_pcm_all[max(0, start): min(len(audio_pcm_all), end)]
        if pad_left > 0 or pad_right > 0:
            window = np.pad(window, (pad_left, pad_right), mode="constant")
        mel = extractor.extract_mel_window(window, target_steps=16)
        stds.append(float(mel.std()))
        seq += 1

    # 1. 严格帧数校验
    assert seq == total_frames
    assert len(stds) == total_frames
    # 2. 真实声学 Mel 特征校验：无全零，平均标准差饱满
    assert all(s > 1.0 for s in stds)
    assert np.mean(stds) > 2.0
