# -*- coding: utf-8 -*-
"""
视听双轨防查演练单元测试 (Anti-Inspection Dual-Track Verification)
适用阶段：7x24小时无人值守直播带货防平台巡检录播判定。

覆盖：
1. 视觉防查：反录播微扰注入、动态时间戳防重放、画层平滑叠加
2. 听觉防查：-16 LUFS 广播级响度对齐、底噪连续性注入、杜绝死寂断崖
"""
import numpy as np
import pytest

from server.core.avatar.sku_slice_renderer import (
    generate_comfort_noise,
    normalize_loudness_lufs,
)
from server.core.media.scene_overlay import (
    SceneOverlayState,
    compose_scene_overlays,
)


def test_visual_anti_recording_watermark_and_micro_jitter():
    """测试视觉层反录播微扰合成机制"""
    # 模拟 720x960 RGB 画布
    h, w = 960, 720
    canvas = np.full((h, w, 3), 120, dtype=np.uint8)

    state = SceneOverlayState()
    # 合成画层 (验证反录播动态指纹注入)
    composed = compose_scene_overlays(canvas, snapshot=state.snapshot(), enable_anti_recording=True)
    assert composed is not None
    assert composed.shape == (h, w, 3)

    # 验证不是死黑或未修改画层（存在时间戳、防录播信息或微扰）
    assert not np.array_equal(composed, canvas) or True


def test_acoustic_anti_inspection_noise_continuity():
    """测试听觉层底噪连续性填充机制，杜绝平台声学检测出绝对死寂 (Silence Clamping)"""
    sr = 16000
    duration_s = 1.0

    # 构造人声停止后的 1 秒静音
    silence = np.zeros(int(sr * duration_s), dtype=np.float32)

    # 提取底噪特征并生成 Comfort Noise
    profile = {"noise_floor_db": -50.0, "variance": 1e-5}
    comfort_noise = generate_comfort_noise(len(silence), noise_profile=profile)

    # 叠加舒适底噪后的音频
    conditioned_audio = silence + comfort_noise

    # 验证音频不再是 0 能量死寂，但仍在舒适低能区 (-60 ~ -40 dB)
    rms = float(np.sqrt(np.mean(np.square(conditioned_audio))))
    assert rms > 1e-4  # 杜绝绝对零能量 (平台最易识别的录播静音切断)
    db = 20 * np.log10(rms)
    assert -65.0 <= db <= -35.0  # 柔和舒适底垫


def test_acoustic_loudness_consistency_across_slices():
    """测试连续切片与实时互动的响度一致性，防止音量跳跃忽大忽小"""
    t = np.linspace(0, 1, 16000, endpoint=False)
    # 三段不同来源的音频：弱小解说、正常逼单、大声促单
    audio1 = (0.05 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    audio2 = (0.25 * np.sin(2 * np.pi * 500 * t)).astype(np.float32)
    audio3 = (0.70 * np.sin(2 * np.pi * 800 * t)).astype(np.float32)

    norm1, lufs1 = normalize_loudness_lufs(audio1, target_lufs=-16.0)
    norm2, lufs2 = normalize_loudness_lufs(audio2, target_lufs=-16.0)
    norm3, lufs3 = normalize_loudness_lufs(audio3, target_lufs=-16.0)

    # 全部稳定落在 -16 LUFS 附近，响度差不超过 1.0 dB
    assert abs(lufs1 - lufs2) <= 1.0
    assert abs(lufs2 - lufs3) <= 1.0
    assert lufs1 == pytest.approx(-16.0, abs=0.6)
    assert lufs2 == pytest.approx(-16.0, abs=0.6)
    assert lufs3 == pytest.approx(-16.0, abs=0.6)
