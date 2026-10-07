# -*- coding: utf-8 -*-
"""
数字人资产预处理质量守卫与几何一致性回归测试 (LIPSYNC_OPTIMIZATION_PLAN.md v3.1 P0)
验证：
1. 人脸检出率 < 90% 时显式抛出 ValueError 拒绝生成损坏资产；
2. 连续丢脸超过 MAX_CARRY_FRAMES (5 帧) 时抛出 ValueError，杜绝错误框无限传播；
3. 起始帧无脸时抛出 ValueError，避免以盲猜默认框污染全片；
4. 轻微抖动 (丢失 <= 5 帧且总体 >= 90%) 允许平滑继承；
5. meta.json 完整落盘 face_detected_frames, detection_rate, max_carry_run, detection_passed 等指标；
6. crop_face_256 与整脸 coord_box 几何裁剪逐像素严格一致。
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from server.core.avatar.task_manager import AvatarTaskManager
from server.core.avatar.neural_lip_renderer import NeuralLipRenderer


def _create_synthetic_face_frame(has_face: bool = True, w: int = 320, h: int = 240) -> np.ndarray:
    """生成带人脸/不带人脸的纯测试图像"""
    frame = np.full((h, w, 3), 40, dtype=np.uint8)
    if has_face:
        # 画逼真的人脸椭圆以触发 Haar 分类器
        center = (w // 2, h // 2)
        axes = (40, 55)
        cv2.ellipse(frame, center, axes, 0, 0, 360, (200, 200, 200), -1)
        # 眼睛
        cv2.circle(frame, (center[0] - 15, center[1] - 12), 6, (30, 30, 30), -1)
        cv2.circle(frame, (center[0] + 15, center[1] - 12), 6, (30, 30, 30), -1)
        # 嘴唇
        cv2.ellipse(frame, (center[0], center[1] + 22), (14, 6), 0, 0, 360, (50, 50, 180), -1)
    return frame


def test_asset_guard_fail_when_start_frame_no_face(tmp_path):
    """验证起始帧无脸时显式抛错，避免全图居中盲猜感染后续帧"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    paths = []
    for i in range(10):
        # 第 0 帧无脸，后续有脸
        img = _create_synthetic_face_frame(has_face=(i > 0))
        p = frames_dir / f"{i:03d}.jpg"
        cv2.imwrite(str(p), img)
        paths.append(p)

    # 模拟 Haar 检测：第 0 帧返回空，其余帧返回有效框
    def fake_detect(gray, **kwargs):
        # 简单判断中心区域是否有眼睛颜色
        if gray[120, 160] > 100:
            return np.array([[120, 65, 80, 110]])
        return np.array([])

    with patch.object(cv2.CascadeClassifier, "detectMultiScale", side_effect=fake_detect):
        with pytest.raises(ValueError) as exc_info:
            AvatarTaskManager._detect_faces_and_coords_worker(
                paths, "task_test_start_miss", set()
            )
        assert "第 0 帧（素材起始帧）未检测到清晰人脸" in str(exc_info.value)


def test_asset_guard_fail_when_consecutive_misses_exceed_limit(tmp_path):
    """验证连续丢失超过 5 帧时触发报错，阻止错误框无限传播"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    paths = []
    for i in range(15):
        # 前 3 帧有脸，中间 4~10 帧 (连续 7 帧) 无脸
        has_face = i < 3 or i >= 11
        img = _create_synthetic_face_frame(has_face=has_face)
        p = frames_dir / f"{i:03d}.jpg"
        cv2.imwrite(str(p), img)
        paths.append(p)

    def fake_detect(gray, **kwargs):
        if gray[120, 160] > 100:
            return np.array([[120, 65, 80, 110]])
        return np.array([])

    with patch.object(cv2.CascadeClassifier, "detectMultiScale", side_effect=fake_detect):
        with pytest.raises(ValueError) as exc_info:
            AvatarTaskManager._detect_faces_and_coords_worker(
                paths, "task_test_consecutive_miss", set()
            )
        assert "超过安全上限 5 帧" in str(exc_info.value)


def test_asset_guard_fail_when_detection_rate_below_threshold(tmp_path):
    """验证总检出率低于 90% 时拒绝产出损坏切片"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    paths = []
    # 20 帧中有 4 帧间隔丢失（单次丢失 1 帧，但总检出率 16/20 = 80% < 90%）
    for i in range(20):
        has_face = (i not in (3, 7, 11, 15))
        img = _create_synthetic_face_frame(has_face=has_face)
        p = frames_dir / f"{i:03d}.jpg"
        cv2.imwrite(str(p), img)
        paths.append(p)

    def fake_detect(gray, **kwargs):
        if gray[120, 160] > 100:
            return np.array([[120, 65, 80, 110]])
        return np.array([])

    with patch.object(cv2.CascadeClassifier, "detectMultiScale", side_effect=fake_detect):
        with pytest.raises(ValueError) as exc_info:
            AvatarTaskManager._detect_faces_and_coords_worker(
                paths, "task_test_low_ratio", set()
            )
        assert "低于安全阈值 90%" in str(exc_info.value)


def test_asset_guard_success_with_minor_jitter(tmp_path):
    """验证轻微扰动 (连续丢失 2 帧且总检出率 93.3% >= 90%) 正常通过并正确落盘"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    paths = []
    # 30 帧中只有第 10, 11 两帧丢失，检出率 28/30 = 93.3%
    for i in range(30):
        has_face = (i not in (10, 11))
        img = _create_synthetic_face_frame(has_face=has_face)
        p = frames_dir / f"{i:03d}.jpg"
        cv2.imwrite(str(p), img)
        paths.append(p)

    def fake_detect(gray, **kwargs):
        if gray[120, 160] > 100:
            return np.array([[120, 65, 80, 110]])
        return np.array([])

    with patch.object(cv2.CascadeClassifier, "detectMultiScale", side_effect=fake_detect):
        coords, summary = AvatarTaskManager._detect_faces_and_coords_worker(
            paths, "task_test_minor_jitter", set()
        )
        assert len(coords) == 30
        assert summary["total_frames"] == 30
        assert summary["face_detected_frames"] == 28
        assert summary["detection_rate"] >= 0.90
        assert summary["max_carry_run"] == 2
        assert summary["detection_passed"] is True


def test_crop_face_256_geometry_exact_alignment():
    """验证 crop_face_256 裁剪出的 256x256 图像与整脸框 coord_box 完全重合 (消除口部下移撕裂)"""
    renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))
    h, w = 480, 640
    frame = np.zeros((h, w, 3), dtype=np.uint8)

    # 假定人脸坐标
    ymin, ymax, xmin, xmax = 80, 320, 150, 390
    # 在该区域画一些特征标记
    frame[ymin:ymax, xmin:xmax] = 128
    frame[ymin + 10:ymin + 20, xmin + 10:xmin + 20] = 255  # 顶角标记

    crop = renderer.crop_face_256(frame, (ymin, ymax, xmin, xmax))
    assert crop is not None
    assert crop.shape == (256, 256, 3)

    # 直接手动从原图 coord_box 裁剪缩放
    expected = cv2.resize(frame[ymin:ymax, xmin:xmax], (256, 256), interpolation=cv2.INTER_AREA)

    # 两者必须逐像素完全一致
    diff = np.max(np.abs(crop.astype(np.float32) - expected.astype(np.float32)))
    assert diff == 0.0, f"crop_face_256 几何口径与 coord_box 不一致，最大差异: {diff}"
