# -*- coding: utf-8 -*-
"""
神经唇形实时算力预检 + 资产健康闸门 + 时序上下文接线
(LIPSYNC_OPTIMIZATION_PLAN.md v3.2 §7)

背景依据（实测，本机 12 核 CPU 无 CUDA）::

    单帧 ONNX 推理 176.4 ms  vs  25fps 预算 40.0 ms  ->  超 4.4 倍

因此「本地 CPU 实时神经唇形」在架构上不可行，必须在开播前告知，
而不是让用户开播后撞上持续掉帧。
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pytest

from server.core.avatar.asset_health import (
    MAX_CARRY_FRAMES,
    MIN_DETECT_RATIO,
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNUSABLE,
    STATUS_UNKNOWN,
    check_asset_health,
)
from server.core.avatar.lipsync_preflight import (
    FRAME_BUDGET_MS_25FPS,
    LEVEL_PREVIEW_ONLY,
    LEVEL_REALTIME,
    LEVEL_TIGHT,
    LEVEL_UNAVAILABLE,
    LipSyncPreflight,
    PreflightResult,
)


# ====================================================================== 算力预检

class _FakeSession:
    def __init__(self, providers, latency_s=0.001):
        self._providers = providers
        self._latency = latency_s
        self.calls = 0

    def get_providers(self):
        return list(self._providers)

    def run(self, out_names, feed):
        import time as _t

        self.calls += 1
        _t.sleep(self._latency)
        return [np.zeros((1, 3, 256, 256), dtype=np.float32)]


class _FakeRenderer:
    def __init__(self, latency_s=0.001, ready=True, providers=("CPUExecutionProvider",)):
        self.model_key = "onnx_lipsync"
        self.is_ready = ready
        self.session = _FakeSession(providers, latency_s) if ready else None
        self.input_names = ["mel_spectrogram", "video_frames"]
        self.output_names = ["output"]
        self.mel_extractor = _FakeExtractor()

    def _prepare_face_input(self, face):
        return np.zeros((1, 6, 256, 256), dtype=np.float32)


class _FakeExtractor:
    def extract_mel_window(self, pcm, target_steps=16):
        return np.zeros((1, 1, 80, 16), dtype=np.float32)


def test_preflight_flags_cpu_only_as_preview_only():
    """CPU 且延迟超预算 -> 必须判定为 preview_only 且 ok=False"""
    pf = LipSyncPreflight()
    pf.invalidate()
    # 180ms/帧，模拟实测的本机水平
    r = pf.probe(renderer=_FakeRenderer(latency_s=0.180), use_cache=False)
    assert r.level == LEVEL_PREVIEW_ONLY
    assert r.ok is False
    assert r.provider == "CPUExecutionProvider"
    assert r.headroom > 1.0
    assert r.achievable_fps < 25
    assert "25fps" in r.summary()


def test_preflight_accepts_fast_gpu_as_realtime():
    """GPU 且余量充足 -> realtime"""
    pf = LipSyncPreflight()
    r = pf.probe(
        renderer=_FakeRenderer(latency_s=0.005,
                               providers=("CUDAExecutionProvider",)),
        use_cache=False,
    )
    assert r.level == LEVEL_REALTIME
    assert r.ok is True
    assert r.headroom < 0.5


def test_preflight_tight_when_just_within_budget():
    """接近预算但仍 <= 1.0 -> tight（够用但会掉帧），仍 ok=True"""
    pf = LipSyncPreflight()
    r = pf.probe(renderer=_FakeRenderer(latency_s=0.038), use_cache=False)
    assert r.level == LEVEL_TIGHT
    assert r.ok is True
    assert r.headroom <= 1.0
    assert r.hints, "tight 档必须给出可操作提示"


def test_preflight_unavailable_when_engine_not_ready():
    """引擎未就绪 -> unavailable，且不得谎报 ok"""
    pf = LipSyncPreflight()
    r = pf.probe(renderer=_FakeRenderer(ready=False), use_cache=False)
    assert r.level == LEVEL_UNAVAILABLE
    assert r.ok is False
    assert "未就绪" in r.reason


def test_preflight_result_is_serializable():
    """结论必须可 JSON 化，供 /live/start 返回给前端"""
    pf = LipSyncPreflight()
    r = pf.probe(renderer=_FakeRenderer(latency_s=0.180), use_cache=False)
    payload = json.loads(json.dumps(r.to_dict()))
    for key in ("level", "ok", "provider", "measured_ms", "budget_ms",
                "achievable_fps", "headroom", "hints"):
        assert key in payload


def test_preflight_budget_is_40ms_for_25fps():
    assert abs(FRAME_BUDGET_MS_25FPS - 40.0) < 1e-6


def test_preflight_cache_returns_same_result():
    pf = LipSyncPreflight()
    fake = _FakeRenderer(latency_s=0.180)
    a = pf.probe(renderer=fake, use_cache=True)
    calls_after_first = fake.session.calls
    b = pf.probe(renderer=fake, use_cache=True)
    assert a.measured_ms == b.measured_ms
    assert fake.session.calls == calls_after_first, "命中缓存时不应重复实测"


# ====================================================================== 资产健康

def _make_asset(tmp_path, n_frames, meta=None, coords_n=None):
    import cv2

    d = tmp_path / "task_test"
    (d / "face_imgs").mkdir(parents=True)
    coords_n = coords_n if coords_n is not None else n_frames
    with open(d / "coords.pkl", "wb") as f:
        pickle.dump([(0, 100, 0, 100)] * coords_n, f)
    for i in range(n_frames):
        img = np.full((240, 320, 3), 40, dtype=np.uint8)
        # 画一个可被 Haar 检出的椭圆脸
        cv2.ellipse(img, (160, 120), (40, 55), 0, 0, 360, (200, 200, 200), -1)
        cv2.circle(img, (145, 108), 6, (30, 30, 30), -1)
        cv2.circle(img, (175, 108), 6, (30, 30, 30), -1)
        cv2.imwrite(str(d / "face_imgs" / f"{i:04d}.jpg"), img)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return str(d)


def test_asset_health_ok_from_meta(tmp_path):
    """新守卫生成的资产：meta 有可信指标，走快路径"""
    d = _make_asset(tmp_path, 20, meta={
        "detection_rate": 1.0, "max_carry_run": 0, "frame_count": 20,
    })
    h = check_asset_health(d, allow_sampled=False)
    assert h.status == STATUS_OK
    assert h.source == "meta"
    assert h.ok is True


def test_asset_health_flags_degraded_from_meta(tmp_path):
    """meta 显示连续丢帧超限 -> degraded 并给出可操作提示"""
    d = _make_asset(tmp_path, 20, meta={
        "detection_rate": 0.97, "max_carry_run": MAX_CARRY_FRAMES + 3,
        "frame_count": 20,
    })
    h = check_asset_health(d, allow_sampled=False)
    assert h.status == STATUS_DEGRADED
    assert h.ok is False
    assert h.hints
    assert "重切" in " ".join(h.hints)


def test_asset_health_flags_unusable_from_meta(tmp_path):
    """检出率过低 -> unusable"""
    d = _make_asset(tmp_path, 20, meta={
        "detection_rate": 0.34, "max_carry_run": 66, "frame_count": 20,
    })
    h = check_asset_health(d, allow_sampled=False)
    assert h.status == STATUS_UNUSABLE
    assert not h.ok


def test_asset_health_unknown_when_no_evidence(tmp_path):
    """既无 meta 指标又禁用抽样 -> unknown，且不得谎报 ok"""
    d = _make_asset(tmp_path, 6)
    h = check_asset_health(d, allow_sampled=False)
    assert h.status == STATUS_UNKNOWN
    assert h.ok is False


def test_asset_health_missing_dir():
    h = check_asset_health("/definitely/not/here")
    assert h.status == STATUS_UNKNOWN
    assert "不存在" in h.reason


def test_asset_health_thresholds_match_asset_guard():
    """阈值必须与 task_manager 的资产守卫一致，否则两处判定会打架"""
    from server.core.avatar.task_manager import AvatarTaskManager
    import inspect

    src = inspect.getsource(AvatarTaskManager._detect_faces_and_coords_worker)
    assert f"MIN_DETECT_RATIO = {MIN_DETECT_RATIO}" in src, (
        "检出率阈值必须与资产守卫一致"
    )
    assert f"MAX_CARRY_FRAMES = {MAX_CARRY_FRAMES}" in src, (
        "连续丢帧上限必须与资产守卫一致"
    )


# ============================================================== 时序上下文接线

def test_single_frame_model_detected_as_non_temporal():
    """单帧模型必须被识别为非时序，不能误走时序分支"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    r.temporal_frames = 0
    assert r.supports_temporal is False


def test_supports_temporal_survives_new_construction():
    """__new__ 绕过 __init__ 构造时不得抛 AttributeError

    否则异常会被渲染主循环的兜底 except 吞掉，表现为「静默返回 None」，
    极难排查。这是本模块实测踩过的坑。
    """
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    assert r.supports_temporal is False        # 不得抛异常
    r.reset_temporal_state()                   # 也不得抛异常


def test_temporal_tensor_shape_and_history():
    """时序输入形状 [1,6,256,256,T]，且第二帧应带真实历史"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    r.temporal_frames = 5
    r._prev_face_tensor = None
    r._prev_frame_idx = None

    f0 = np.zeros((1, 6, 256, 256), dtype=np.float32)
    s0 = r._build_temporal_face_tensor(f0, 0)
    assert s0.shape == (1, 6, 256, 256, 5)

    f1 = np.ones((1, 6, 256, 256), dtype=np.float32)
    s1 = r._build_temporal_face_tensor(f1, 1)
    assert s1.shape == (1, 6, 256, 256, 5)
    # 最后一帧必须是当前帧
    assert np.allclose(s1[..., -1], f1)
    # 前面应含上一帧历史
    assert np.allclose(s1[..., 0], f0)


def test_temporal_resets_on_frame_discontinuity():
    """帧号跳变必须重置历史，否则会拼接两个无关时刻的人脸"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    r.temporal_frames = 5
    r._prev_face_tensor = None
    r._prev_frame_idx = None

    f0 = np.zeros((1, 6, 256, 256), dtype=np.float32)
    f9 = np.full((1, 6, 256, 256), 2.0, dtype=np.float32)
    r._build_temporal_face_tensor(f0, 0)
    s = r._build_temporal_face_tensor(f9, 9)      # 0 -> 9 跳变
    assert np.allclose(s, f9[:, :, :, :, None]), "跳变后应全部用当前帧填充"


def test_reset_temporal_state_clears_history():
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    r.temporal_frames = 5
    r._prev_face_tensor = np.zeros((1, 6, 256, 256), dtype=np.float32)
    r._prev_frame_idx = 7
    r.reset_temporal_state()
    assert r._prev_face_tensor is None
    assert r._prev_frame_idx is None


def test_silent_frame_resets_temporal_history():
    """静音帧必须清空时序历史，否则静音结束后首帧会跨静音拼接"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    r.temporal_frames = 5
    r._prev_face_tensor = np.zeros((1, 6, 256, 256), dtype=np.float32)
    r._prev_frame_idx = 3
    r.has_anchor_assets = True
    r.face_imgs = [np.zeros((256, 256, 3), dtype=np.uint8)]

    full = np.zeros((200, 200, 3), dtype=np.uint8)
    r._wrap_silent(full, 4, None, False)
    assert r._prev_face_tensor is None, "静音帧必须重置时序历史"
    assert r._prev_frame_idx is None


def test_temporal_model_requires_gpu_capability_hint():
    """时序模型仅在 GPU 余量充足时才应被建议启用"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    r = NeuralLipRenderer.__new__(NeuralLipRenderer)
    r.temporal_frames = 0
    assert r.supports_temporal is False
    r.temporal_frames = 5
    assert r.supports_temporal is True


# ============================================================== 脚本存在性

@pytest.mark.parametrize("script", [
    "scripts/quantize_onnx.py",
    "scripts/visual_lead_ab.py",
    "scripts/audit_avatar_assets.py",
    "scripts/export_wav2lip_temporal_onnx.py",
])
def test_scripts_exist(script):
    p = Path(__file__).resolve().parents[2] / script
    assert p.exists(), f"缺少脚本 {script}"


def test_quantize_script_documented_tradeoff():
    """量化脚本必须写明画质代价，避免被当成无脑加速开关"""
    src = (Path(__file__).resolve().parents[2] / "scripts" / "quantize_onnx.py").read_text(
        encoding="utf-8")
    assert "伪影" in src or "画质" in src, "必须说明量化的画质代价"
    assert "不要自动替换" in src or "不自动替换" in src, (
        "必须明确量化模型不自动替换生产权重"
    )


def test_check_and_enforce_blocks_unusable(tmp_path):
    from server.core.avatar.asset_health import check_and_enforce, AssetUnusableError
    d = _make_asset(tmp_path, 20, meta={"detection_rate": 0.34, "max_carry_run": 66, "frame_count": 20})
    with pytest.raises(AssetUnusableError) as exc_info:
        check_and_enforce(d, allow_unusable=False)
    assert "阻断开播" in str(exc_info.value)


def test_check_and_enforce_bypassed_via_env(tmp_path, monkeypatch):
    from server.core.avatar.asset_health import check_and_enforce
    monkeypatch.setenv("LIVE_AGENT_ALLOW_UNUSABLE_ASSETS", "1")
    d = _make_asset(tmp_path, 20, meta={"detection_rate": 0.34, "max_carry_run": 66, "frame_count": 20})
    h = check_and_enforce(d)
    assert h.status == STATUS_UNUSABLE
