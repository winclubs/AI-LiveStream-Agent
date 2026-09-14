import numpy as np
import pytest
from server.core.media.procedural_renderer import (
    audio_samples_to_viseme,
    audio_rms_to_mouth,
    synth_frame,
    generate_default_portrait
)
from server.adapters.media.musetalk_driver import (
    MuseTalkMediaDriver,
    ProceduralAvatarDriver,
    Viseme2DMediaDriver
)


def test_audio_samples_to_viseme_extraction():
    """测试基于音频采样的 Viseme 多维口型提取 (open 与 form)"""
    # 1. 静音全零测试
    silence = np.zeros(1600, dtype=np.float32)
    m_open, m_form = audio_samples_to_viseme(silence)
    assert m_open == 0.0
    assert m_form == 0.0

    # 2. 低频纯正弦波 (模拟元音 /a/, /o/，低频成分占主导)
    t = np.linspace(0, 0.1, 1600, endpoint=False)
    low_freq = (0.2 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    m_open_low, m_form_low = audio_samples_to_viseme(low_freq)
    assert m_open_low > 0.1
    # 低频主导时，form 偏向中低 (圆唇或中立)
    assert m_form_low <= 0.2

    # 3. 高频信号 (模拟摩擦音 /s/、前元音 /i/，一阶差分高频能量显著)
    high_freq = (0.2 * np.sin(2 * np.pi * 3500 * t)).astype(np.float32)
    m_open_high, m_form_high = audio_samples_to_viseme(high_freq)
    assert m_open_high > 0.1
    # 高频成分主导时，form 显著偏向展唇正值
    assert m_form_high > m_form_low


def test_synth_frame_with_viseme_form():
    """测试多维口型参数合成为 RGB 帧且产生真实像素几何形态差异"""
    portrait = generate_default_portrait(720, 960)
    assert portrait.shape == (960, 720, 3)

    # 1. 普通开口测试
    frame_normal = synth_frame(portrait, 720, 960, t=1.0, mouth_open=0.5, mouth_form=0.0)
    assert frame_normal.shape == (960, 720, 3)

    # 2. 展唇开口测试 (/i/，水平拉伸、高度较扁)
    frame_spread = synth_frame(portrait, 720, 960, t=1.0, mouth_open=0.4, mouth_form=0.8)
    assert frame_spread.shape == (960, 720, 3)

    # 3. 圆唇开口测试 (/u/，水平收拢、高度收拢成 O 型)
    frame_round = synth_frame(portrait, 720, 960, t=1.0, mouth_open=0.4, mouth_form=-0.8)
    assert frame_round.shape == (960, 720, 3)

    # 4. 严谨断言：展唇与圆唇在嘴部核心 ROI 区域必须产生绝对矩阵像素差异，杜绝无意义空转
    mouth_roi_spread = frame_spread[int(960 * 0.50):int(960 * 0.75), int(720 * 0.35):int(720 * 0.65), :]
    mouth_roi_round = frame_round[int(960 * 0.50):int(960 * 0.75), int(720 * 0.35):int(720 * 0.65), :]
    abs_diff = np.abs(mouth_roi_spread.astype(np.int32) - mouth_roi_round.astype(np.int32))
    assert np.sum(abs_diff) > 0, "展唇与圆唇在嘴部 ROI 区域未产生任何像素矩阵变化"
    assert np.max(abs_diff) > 10, "展唇与圆唇像素差异过小，未达到有效视觉形变阈值"


def test_driver_alias_compatibility():
    """测试 ProceduralAvatarDriver 与 Viseme2DMediaDriver 别名与原类完全等价"""
    assert ProceduralAvatarDriver is MuseTalkMediaDriver
    assert Viseme2DMediaDriver is MuseTalkMediaDriver

    driver = ProceduralAvatarDriver()
    assert hasattr(driver, "current_mouth_form")
    assert hasattr(driver, "target_mouth_form")
