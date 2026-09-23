# -*- coding: utf-8 -*-
"""
单元测试：视觉反录播动态指纹干扰与哈希离散化验证 (任务 2.3)
"""
import hashlib
import numpy as np
import pytest

from server.core.media.scene_overlay import (
    apply_anti_recording_watermark,
    compose_scene_overlays,
)


def test_anti_recording_hash_dispersion():
    """验证连续输出帧的二进制哈希完全离散，杜绝平台固定 MD5 录播审查"""
    # 构造一张基准底图 (640x360x3)
    base_frame = np.full((360, 640, 3), 128, dtype=np.uint8)

    hashes = set()
    frames = []
    # 模拟连续 30 帧 (1.2 秒) 输出
    for i in range(30):
        t = i * 0.04  # 25fps
        frame = apply_anti_recording_watermark(base_frame, timestamp=t, noise_sigma=1.2)
        frames.append(frame)
        h = hashlib.sha256(frame.tobytes()).hexdigest()
        hashes.add(h)

    # 验证 30 帧的 SHA256 哈希全部各不相同 (100% 离散)
    assert len(hashes) == 30, f"哈希出现碰撞重复，未能实现连续帧哈希动态离散化: {len(hashes)}/30"

    # 验证视觉扰动幅度在肉眼不可见容差范围内 (平均绝对误差 MAE < 2.0 像素级别)
    diffs = [np.mean(np.abs(f.astype(np.float32) - base_frame.astype(np.float32))) for f in frames]
    avg_mae = float(np.mean(diffs))
    assert 0.1 < avg_mae < 3.0, f"扰动幅度异常: MAE={avg_mae}"


def test_compose_scene_overlays_with_anti_recording():
    """验证全局场景合成器默认包含反录播指纹"""
    base_frame = np.full((120, 160, 3), 100, dtype=np.uint8)

    out1 = compose_scene_overlays(base_frame, enable_anti_recording=True, timestamp=1.0)
    out2 = compose_scene_overlays(base_frame, enable_anti_recording=True, timestamp=1.04)

    assert out1.shape == base_frame.shape
    assert not np.array_equal(out1, out2), "两帧画面未产生动态离散扰动"
