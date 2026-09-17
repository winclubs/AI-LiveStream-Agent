# -*- coding: utf-8 -*-
"""
ASR 与 VAD 语音识别引擎单元测试
验证 VAD 能量端点检测、ASR 接口空值保护与异常容错。
"""
import pytest
import numpy as np
from server.core.audio.asr_engine import is_voice_active, transcribe_audio_bytes


def test_is_voice_active_silence():
    """验证纯静音音频的 VAD 检测"""
    # 纯零 静音 PCM (16000 采样率，200ms = 3200 样本 = 6400 字节)
    silence = np.zeros(3200, dtype=np.int16).tobytes()
    assert is_voice_active(silence, energy_threshold=300.0) is False


def test_is_voice_active_noise_speech():
    """验证有声音/正弦波音频的 VAD 检测"""
    # 构造幅度 2000 的 440Hz 正弦波
    t = np.linspace(0, 0.2, 3200, endpoint=False)
    sine_wave = (np.sin(2 * np.pi * 440 * t) * 2000).astype(np.int16)
    audio_bytes = sine_wave.tobytes()
    assert is_voice_active(audio_bytes, energy_threshold=300.0) is True


def test_is_voice_active_empty_invalid():
    """验证空数据与畸变数据的异常保护"""
    assert is_voice_active(b"") is False
    assert is_voice_active(b"short") is False


def test_transcribe_audio_bytes_empty():
    """验证空音频转录安全返回空字符串"""
    assert transcribe_audio_bytes(b"") == ""
    assert transcribe_audio_bytes(b"12345678") == ""


def test_transcribe_audio_bytes_mock_fallback():
    """验证在未部署大模型权重环境下的安全兜底机制"""
    dummy_wav = np.zeros(1600, dtype=np.int16).tobytes()
    text = transcribe_audio_bytes(dummy_wav)
    assert isinstance(text, str)
