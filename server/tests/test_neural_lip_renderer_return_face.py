# -*- coding: utf-8 -*-
"""NeuralLipRenderer.return_face 契约测试"""
import numpy as np
import pytest


class _FakeSession:
    def __init__(self):
        self._calls = 0

    def get_inputs(self):
        class _I:
            def __init__(self, name, shape):
                self.name = name
                self.shape = shape

        return [_I("face", [1, 6, 256, 256]), _I("mel", [1, 1, 80, 16])]

    def get_outputs(self):
        class _O:
            def __init__(self, name):
                self.name = name

        return [_O("rendered")]

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, output_names, feed_dict):
        self._calls += 1
        # 返回一张与输入同尺寸的随机人脸 (float [0,1])
        return [np.random.rand(1, 3, 256, 256).astype(np.float32)]


def _make_renderer_with_fake_session(monkeypatch):
    from server.core.avatar import neural_lip_renderer as mod

    monkeypatch.setattr(mod, "ORT_AVAILABLE", True)
    monkeypatch.setattr(mod, "ort", type("ort", (), {"SessionOptions": object}))
    renderer = mod.NeuralLipRenderer.__new__(mod.NeuralLipRenderer)
    renderer.model_key = "onnx_lipsync"
    renderer.session = _FakeSession()
    renderer.is_ready = True
    renderer.mel_extractor = mod.MelFeatureExtractor()
    renderer.input_names = ["face", "mel"]
    renderer.output_names = ["rendered"]
    renderer.face_input_shape = [1, 6, 256, 256]
    renderer.audio_input_shape = [1, 1, 80, 16]
    renderer.current_anchor_dir = None
    renderer.coords = [(0, 100, 0, 100)]
    renderer.face_imgs = [np.zeros((256, 256, 3), dtype=np.uint8)]
    renderer.full_imgs = []
    renderer.has_anchor_assets = True
    return renderer


def test_return_face_yields_full_and_256(monkeypatch):
    renderer = _make_renderer_with_fake_session(monkeypatch)
    full = np.zeros((200, 200, 3), dtype=np.uint8)
    pcm = (np.sin(np.linspace(0, 40 * np.pi, 3200)) * 0.3).astype(np.float32)
    result = renderer.render_lip_frame(full, 0, pcm, mouth_open=0.8, return_face=True)
    assert isinstance(result, tuple) and len(result) == 2
    blended, face = result
    assert blended.shape[:2] == (200, 200)
    assert face.shape[:2] == (256, 256)


def test_default_return_face_backward_compatible(monkeypatch):
    renderer = _make_renderer_with_fake_session(monkeypatch)
    full = np.zeros((200, 200, 3), dtype=np.uint8)
    pcm = (np.sin(np.linspace(0, 40 * np.pi, 3200)) * 0.3).astype(np.float32)
    result = renderer.render_lip_frame(full, 0, pcm, mouth_open=0.8)
    assert isinstance(result, np.ndarray)  # 单一帧，与既有调用方一致


def test_silent_frame_returns_original_and_face(monkeypatch):
    renderer = _make_renderer_with_fake_session(monkeypatch)
    full = np.zeros((200, 200, 3), dtype=np.uint8)
    full[50:90, 20:80] = 255
    silent = np.zeros(3200, dtype=np.float32)
    blended, face = renderer.render_lip_frame(full, 0, silent, mouth_open=0.0, return_face=True)
    assert np.array_equal(blended, full)  # 静音不改变原帧
    assert face.shape[:2] == (256, 256)
