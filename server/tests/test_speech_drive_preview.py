# -*- coding: utf-8 -*-
"""试播台词驱动编排服务测试"""
import math
from pathlib import Path

import numpy as np
import pytest


def test_resample_to_16k_mono_passthrough():
    from server.core.avatar.speech_drive_preview import _resample_to_16k_mono

    pcm = np.linspace(-1, 1, 16000, dtype=np.float32)
    out = _resample_to_16k_mono(pcm, 16000)
    assert out.dtype == np.float32
    assert np.allclose(out, pcm)


def test_resample_to_16k_mono_downsample_length():
    from server.core.avatar.speech_drive_preview import _resample_to_16k_mono

    pcm = np.linspace(-1, 1, 48000, dtype=np.float32)
    out = _resample_to_16k_mono(pcm, 48000)
    assert abs(len(out) - 16000) <= 1


def test_frame_count_bounds_by_max_frames():
    from server.core.avatar.speech_drive_preview import FPS, MAX_FRAMES, _compute_frame_count

    # 2 秒音频 -> 50 帧
    assert _compute_frame_count(2.0) == 50
    # 20 秒音频 -> 截断为 MAX_FRAMES
    assert _compute_frame_count(20.0) == MAX_FRAMES
    # 零时长
    assert _compute_frame_count(0.0) == 0
    assert FPS == 25
