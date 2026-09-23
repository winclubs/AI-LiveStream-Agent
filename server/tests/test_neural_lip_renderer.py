# -*- coding: utf-8 -*-
"""
真实神经唇形重绘驱动引擎 (NeuralLipRenderer) 单元测试
覆盖：
1. MelFeatureExtractor 80维声学特征切片与padding/裁切；
2. NeuralLipRenderer 资产加载与无模型优雅降级；
3. 人脸 6 通道输入张量与下半脸掩码构建；
4. ROI 高斯羽化回贴融合算法 (_blend_back)；
5. 模拟 ONNX 推理与 render_lip_frame 端到端执行；
6. ProceduralAvatarDriver 主推流管线集成与能力上报透传。
"""
import os
import pickle
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from server.core.avatar.neural_lip_renderer import (
    MelFeatureExtractor,
    NeuralLipRenderer,
)
from server.adapters.media.musetalk_driver import ProceduralAvatarDriver
from server.adapters.media.media_router import MediaDriverRouter


def test_mel_feature_extractor():
    """测试 80 维 Mel 特征提取器提取 [1, 1, 80, 16] 窗口"""
    extractor = MelFeatureExtractor(sample_rate=16000, n_mels=80)

    # 1. 正常 200ms 音频 (3200 采样点)
    pcm = (np.sin(np.linspace(0, 100, 3200)) * 0.5).astype(np.float32)
    mel = extractor.extract_mel_window(pcm, target_steps=16)
    assert mel.shape == (1, 1, 80, 16)
    assert mel.dtype == np.float32
    assert not np.isnan(mel).any()
    assert not np.isinf(mel).any()

    # 2. 超短音频 (如 200 采样点)，自动 padding
    short_pcm = np.ones(200, dtype=np.float32) * 0.1
    mel_short = extractor.extract_mel_window(short_pcm, target_steps=16)
    assert mel_short.shape == (1, 1, 80, 16)

    # 3. 超长音频 (如 16000 采样点)，自动裁切中心 16 步长
    long_pcm = np.ones(16000, dtype=np.float32) * 0.1
    mel_long = extractor.extract_mel_window(long_pcm, target_steps=16)
    assert mel_long.shape == (1, 1, 80, 16)


def test_neural_lip_renderer_fallback_when_no_model():
    """测试无模型或模型未就绪时的优雅降级"""
    renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent_model.onnx"))
    assert not renderer.is_ready
    assert not renderer.has_anchor_assets

    full_frame = np.zeros((960, 720, 3), dtype=np.uint8)
    pcm = np.zeros(3200, dtype=np.float32)
    res = renderer.render_lip_frame(full_frame, frame_idx=0, pcm_window=pcm, mouth_open=0.5)
    assert res is None


def test_prepare_face_input():
    """测试 6 通道输入张量构建及下半脸掩码"""
    renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))
    face_img = np.ones((256, 256, 3), dtype=np.uint8) * 200

    tensor = renderer._prepare_face_input(face_img)
    # 形状为 [1, 6, 256, 256]
    assert tensor.shape == (1, 6, 256, 256)
    assert tensor.dtype == np.float32

    # 前 3 通道保持原图归一化值 (约 200/255.0)
    assert np.allclose(tensor[0, :3, :, :], 200.0 / 255.0, atol=1e-3)
    # 后 3 通道上半脸保持原值，下半脸 (y >= 128) 必须全为 0
    assert np.allclose(tensor[0, 3:, :128, :], 200.0 / 255.0, atol=1e-3)
    assert np.all(tensor[0, 3:, 128:, :] == 0.0)


def test_blend_back_seamless():
    """测试自适应高斯羽化融合回贴原帧"""
    renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))

    full_frame = np.zeros((960, 720, 3), dtype=np.uint8)
    rendered_face = np.ones((256, 256, 3), dtype=np.uint8) * 255
    coord_box = (100, 356, 200, 456)  # ymin, ymax, xmin, xmax (256x256)

    blended = renderer._blend_back(full_frame, rendered_face, coord_box)
    assert blended.shape == (960, 720, 3)
    # 嘴唇中心区域应该被混合并具有较高亮度
    center_y = int(100 + 256 * 0.72)
    center_x = 200 + 128
    assert blended[center_y, center_x, 0] > 100
    # 外部未混合区域依然为 0
    assert blended[50, 50, 0] == 0


def test_load_anchor_assets():
    """测试切片资产与坐标清单加载"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        face_dir = root / "face_imgs"
        face_dir.mkdir(parents=True)

        # 写入测试人脸切片
        sample_face = np.zeros((256, 256, 3), dtype=np.uint8)
        cv2.imwrite(str(face_dir / "0000.jpg"), sample_face)
        cv2.imwrite(str(face_dir / "0001.jpg"), sample_face)

        # 写入 coords.pkl
        coords = [(100, 356, 200, 456), (102, 358, 201, 457)]
        with open(root / "coords.pkl", "wb") as f:
            pickle.dump(coords, f)

        renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))
        ok = renderer.load_anchor_assets(root)
        assert ok is True
        assert renderer.has_anchor_assets is True
        assert len(renderer.face_imgs) == 2
        assert len(renderer.coords) == 2


def test_render_lip_frame_end_to_end_mock_session():
    """使用 Mock Session 模拟 ONNX 前向推理并完成端到端渲染"""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        face_dir = root / "face_imgs"
        face_dir.mkdir(parents=True)
        sample_face = np.full((256, 256, 3), 128, dtype=np.uint8)
        cv2.imwrite(str(face_dir / "0000.jpg"), sample_face)
        coords = [(100, 356, 200, 456)]
        with open(root / "coords.pkl", "wb") as f:
            pickle.dump(coords, f)

        renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))
        renderer.load_anchor_assets(root)

        # 构造 mock onnx session
        mock_session = MagicMock()
        renderer.session = mock_session
        renderer.is_ready = True
        renderer.input_names = ["face_sequences", "audio_sequences"]
        renderer.output_names = ["output_face"]

        # 模型输出 [1, 3, 256, 256] 归一化图像
        dummy_out = np.full((1, 3, 256, 256), 0.8, dtype=np.float32)
        mock_session.run.return_value = [dummy_out]

        full_frame = np.zeros((960, 720, 3), dtype=np.uint8)
        pcm = np.ones(3200, dtype=np.float32) * 0.3

        result = renderer.render_lip_frame(
            full_frame=full_frame,
            frame_idx=0,
            pcm_window=pcm,
            mouth_open=0.5,
        )

        assert result is not None
        assert result.shape == (960, 720, 3)
        mock_session.run.assert_called_once()


def test_musetalk_driver_pipeline_neural_integration():
    """测试 ProceduralAvatarDriver 主渲染推流链路对神经唇形重绘的接入与降级"""
    driver = ProceduralAvatarDriver()
    assert hasattr(driver, "lip_renderer")

    # 1. 初始无权重时，neural_lipsync 如实上报 False (ADR-16)
    caps = driver.get_capabilities()["capabilities"]
    assert caps["neural_lipsync"] is False

    # 2. 模拟注入就绪的神经渲染器
    mock_lip = MagicMock()
    mock_lip.is_ready = True
    mock_lip.has_anchor_assets = True
    neural_frame_out = np.full((960, 720, 3), 42, dtype=np.uint8)
    mock_lip.render_lip_frame.return_value = neural_frame_out
    driver.lip_renderer = mock_lip

    # 能力上报动态反映就绪状态
    caps = driver.get_capabilities()["capabilities"]
    assert caps["neural_lipsync"] is True

    # 调用 _synthesize_frame，验证神经重绘帧优先采纳
    test_pcm = np.zeros(3200, dtype=np.float32)
    rendered = driver._synthesize_frame(0.1, mouth_open=0.5, mouth_form=0.0, pcm_window=test_pcm)
    assert np.array_equal(rendered, neural_frame_out)
    mock_lip.render_lip_frame.assert_called_once()

    # 3. 神经重绘单帧抛出异常或返回 None 时，优雅降级
    mock_lip.render_lip_frame.return_value = None
    fallback_frame = driver._synthesize_frame(0.1, mouth_open=0.5, mouth_form=0.0, pcm_window=test_pcm)
    assert fallback_frame is not None
    assert fallback_frame.shape == (960, 720, 3)


def test_media_router_dynamic_capability_reflection():
    """测试 MediaDriverRouter 能够正确穿透并反映底层驱动真实能力"""
    router = MediaDriverRouter()

    # 模拟底模驱动汇报带神经能力
    mock_driver = MagicMock()
    mock_driver.is_speaking = False
    mock_driver.fps = 25
    mock_driver.cv_available = True
    mock_driver.render_backend = "procedural"
    mock_driver.get_capabilities.return_value = {
        "capabilities": {
            "neural_lipsync": True,
            "viseme_lipsync": True,
        }
    }
    mock_driver.get_preview_status.return_value = {
        "driver_type": "procedural",
        "render_backend": "procedural",
        "is_speaking": False,
        "fps": 25,
        "cv_available": True,
        "capabilities": {
            "neural_lipsync": True,
        },
        "neural_lipsync": True,
    }

    router.active_driver = mock_driver
    status = router.get_preview_status()
    assert status["capabilities"]["neural_lipsync"] is True
