# -*- coding: utf-8 -*-
"""P0 修复回归守护：眨眼乒乓循环 + 音画同步对齐。

背景：神经唇形模型只重绘下半脸，眼睛区域 100% 来自底片帧。三处取帧点此前的
`frame_idx % len(face_imgs)` 模运算循环在末帧→首帧产生硬跳变，且把降采样后
残留的闭眼帧变成每 5 秒重放一次的"连续眨眼"指纹。LatentSync 官方 loop_video()
与 MuseTalk 官方 prepare_material() 均采用乒乓镜像循环
(`frame_list + frame_list[::-1]`)，本模块守护这一收敛。

同时守护音画同步三处量化点：帧数 round 对齐音频时长（floor 会漏最多 39ms 尾音）、
preview.mp4 封装去掉会砍流的 -shortest 并显式 cfr。
"""
import pickle
from pathlib import Path

import cv2
import numpy as np
import pytest

from server.core.avatar.action_state_machine import mirror_index


# ---------------------------------------------------------------------------
# 测试资产构造
# ---------------------------------------------------------------------------
def _make_multi_face_asset(root: Path, n: int = 3) -> Path:
    """构造 n 张可区分人脸切片的资产目录（均匀填充 30/60/90... 便于反解选中帧）。

    fake_asset 只有 1 张切片，乒乓与模运算退化成同一结果，无法区分两种循环，
    故此处专门构造多切片资产。
    """
    (root / "face_imgs").mkdir(parents=True, exist_ok=True)
    (root / "full_imgs").mkdir(parents=True, exist_ok=True)
    for i in range(n):
        v = 30 * (i + 1)
        cv2.imwrite(str(root / "face_imgs" / f"{i:04d}.jpg"), np.full((256, 256, 3), v, dtype=np.uint8))
        cv2.imwrite(str(root / "full_imgs" / f"{i:04d}.jpg"), np.full((200, 200, 3), v, dtype=np.uint8))
    with open(root / "coords.pkl", "wb") as f:
        pickle.dump([(0, 200, 0, 200)] * n, f)
    return root


def _base_values(n: int) -> list:
    return [30 * (i + 1) for i in range(n)]


def _corner_mean(img: np.ndarray) -> float:
    """读取 256x256 人脸左上角 [0:30, 0:30] 均值（位于嘴部椭圆蒙版之外）。

    融合蒙版的椭圆中心在 y≈0.72h、下缘到 y≈0.92h，高斯模糊(21x21, sigma≈6~7)
    再向外扩 ~20px。取 [0:30,0:30] 时蒙版恒为 0，该处像素直接反映被选中的
    底片帧，可用于反解循环索引。
    """
    return float(np.asarray(img[:30, :30], dtype=np.float32).mean())


# ---------------------------------------------------------------------------
# P0-2: 帧数按四舍五入对齐音频时长（floor 会漏掉最多 39ms 的尾音）
# ---------------------------------------------------------------------------
def test_compute_frame_count_rounds_to_nearest_frame():
    from server.core.avatar.speech_drive_preview import _compute_frame_count

    # 2.03s * 25 = 50.75 -> 51 帧（floor 会给 50，只覆盖 2.0s，漏 30ms 尾音）
    assert _compute_frame_count(2.03) == 51, "小数部分 >=0.5 个帧周期必须进位"
    # 2.01s * 25 = 50.25 -> 50 帧
    assert _compute_frame_count(2.01) == 50, "小数部分 <0.5 个帧周期必须舍去"
    # 整数倍保持不变（既有契约）
    assert _compute_frame_count(2.0) == 50


# ---------------------------------------------------------------------------
# P0-2: preview.mp4 封装命令——去 -shortest、显式 cfr、framerate 对齐
# ---------------------------------------------------------------------------
def test_build_preview_mp4_cmd_aligns_duration_without_shortest():
    """-shortest 会在最短流结束时砍掉另一路；应改用帧数对齐 + 显式恒定帧率。"""
    from server.core.avatar.speech_drive_preview import FPS, _build_preview_mp4_cmd

    frames_dir = Path("/tmp/sess_pp/full")
    cmd = _build_preview_mp4_cmd(frames_dir, Path("/tmp/sess_pp/audio.mp3"), Path("/tmp/sess_pp/preview.mp4"))

    assert "-shortest" not in cmd, "-shortest 会在最短流结束时截断另一路，应靠帧数对齐保证时长"
    assert cmd[cmd.index("-fps_mode") + 1] == "cfr", "必须显式恒定帧率，避免 muxer 二次改时间戳"
    assert cmd[cmd.index("-framerate") + 1] == str(FPS), "输入帧率必须与项目 FPS 一致"
    assert str(frames_dir / "%d.jpg") in cmd, "帧序列输入路径必须正确"
    assert str(Path("/tmp/sess_pp/audio.mp3")) in cmd, "音频输入路径必须正确"
    assert cmd[-1] == str(Path("/tmp/sess_pp/preview.mp4")), "输出路径必须在命令末尾"


# ---------------------------------------------------------------------------
# P0-1: 本地神经渲染取帧使用乒乓镜像循环
# ---------------------------------------------------------------------------
def test_neural_lip_renderer_uses_pingpong_loop_index(tmp_path):
    """本地神经渲染的底片帧循环必须是乒乓镜像 (0,1,2,1,0,...) 而非模运算 (0,1,2,0,...)。

    模运算在末帧→首帧产生硬跳变，是"连续不停眨眼"的根因。
    """
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    _make_multi_face_asset(tmp_path, n=3)
    base_values = _base_values(3)

    renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))
    renderer.load_anchor_assets(tmp_path)
    assert len(renderer.face_imgs) == 3

    captured: list = []

    class _FakeSession:
        def run(self, output_names, feed_dict):
            captured.append(feed_dict["video_frames"])
            return [np.zeros((1, 3, 256, 256), dtype=np.float32)]

    renderer.session = _FakeSession()
    renderer.is_ready = True
    renderer.input_names = ["video_frames", "mel_spectrogram"]
    renderer.output_names = ["output_face"]

    full_frame = np.zeros((960, 720, 3), dtype=np.uint8)
    pcm = np.ones(3200, dtype=np.float32) * 0.3
    for i in range(7):
        res = renderer.render_lip_frame(full_frame, i, pcm, mouth_open=0.5)
        assert res is not None, f"第 {i} 帧渲染不应失败"

    # 6 通道输入后 3 通道为完整原图（未置零），反解选中的底片帧序号
    def _selected_value(t: np.ndarray) -> float:
        return float(t[0, 3:6, 0:50, 0:50].mean() * 255.0)

    selected = [_selected_value(t) for t in captured]
    # 乒乓期望: mirror_index(i,3) for i in 0..6 = [0,1,2,1,0,1,2]
    expected = [base_values[mirror_index(i, 3)] for i in range(7)]
    assert selected == pytest.approx(expected, abs=1.5), (
        f"底片帧循环不是乒乓镜像: {selected} vs {expected}"
    )
    # 关键反例：模运算在 i=3 会回到 base 30，乒乓应为 60
    assert selected[3] == pytest.approx(base_values[1], abs=1.5)


def test_neural_lip_renderer_silent_frame_uses_pingpong_loop(tmp_path):
    """静音帧走 _wrap_silent 分支，底片帧循环同样必须是乒乓镜像（静音段也保持自然眨眼节律）。"""
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    _make_multi_face_asset(tmp_path, n=3)
    base_values = _base_values(3)

    renderer = NeuralLipRenderer(custom_onnx_path=Path("non_existent.onnx"))
    renderer.load_anchor_assets(tmp_path)

    # 静音帧不进入推理，但仍需 is_ready/session 就绪才能走到 _wrap_silent 分支
    renderer.session = object()
    renderer.is_ready = True

    selected = []
    full_frame = np.zeros((960, 720, 3), dtype=np.uint8)
    silent_pcm = np.zeros(3200, dtype=np.float32)
    for i in range(7):
        res = renderer.render_lip_frame(full_frame, i, silent_pcm, mouth_open=0.0, return_face=True)
        assert res is not None, f"静音第 {i} 帧应返回 (full, face)"
        _full, face256 = res
        selected.append(_corner_mean(face256))

    expected = [base_values[mirror_index(i, 3)] for i in range(7)]
    assert selected == pytest.approx(expected, abs=2.0), (
        f"静音帧底片循环不是乒乓镜像: {selected} vs {expected}"
    )


# ---------------------------------------------------------------------------
# P0-1: 云端 LatentSync 批处理取帧使用乒乓镜像循环（两份部署文档同构）
# ---------------------------------------------------------------------------
def _load_sidecar(doc_name: str):
    from server.tests.test_google_gpu_deploy_script import load_sidecar

    return load_sidecar(doc_name)


@pytest.fixture(params=["google_gpu.md", "intern_gpu.md"])
def sidecar_ns(request):
    return _load_sidecar(request.param)


def test_cloud_render_latentsync_uses_pingpong_loop(sidecar_ns, tmp_path):
    """云端 render_latentsync 的底片帧循环必须是乒乓镜像，与本地渲染收敛一致。

    断言打在**输出帧**上而非 session 入参：云端用 ThreadPoolExecutor 并发渲染，
    入参捕获顺序不确定；output_frames 由 raw_frames 顺序重建，时序是确定的。
    """
    base_values = _base_values(3)
    avatar_dir = sidecar_ns.asset_store.get_avatar_dir("anchor_pingpong")
    face_dir = avatar_dir / "face_imgs"
    face_dir.mkdir(parents=True, exist_ok=True)
    for i, v in enumerate(base_values):
        cv2.imwrite(str(face_dir / f"{i:04d}.jpg"), np.full((256, 256, 3), v, dtype=np.uint8))

    class _FakeSession:
        def run(self, outputs, feed_dict):
            return [np.zeros((1, 3, 256, 256), dtype=np.float32)]

    sidecar_ns.engine.session = _FakeSession()

    # 恰好 7 帧（7 * 640 采样）
    pcm = (np.sin(np.linspace(0, 100 * np.pi, 7 * 640)) * 0.2).astype(np.float32)
    frames = sidecar_ns.engine.render_latentsync("anchor_pingpong", pcm)
    assert len(frames) == 7, f"应渲染 7 帧，实际 {len(frames)}"

    # 采样区域取左上角 [0:30, 0:30]：完全落在嘴部椭圆融合蒙版之外，
    # 该处像素恒等于底片帧原值。
    selected = [_corner_mean(f) for f in frames]

    # 云端末尾对相邻帧做 0.88/0.12 时序防抖平滑：
    # smoothed[i] = 0.88 * base[i] + 0.12 * smoothed[i-1]
    expected: list = []
    prev = None
    for i in range(len(frames)):
        v = base_values[mirror_index(i, 3)]
        expected.append(v if prev is None else 0.88 * v + 0.12 * prev)
        prev = expected[-1]

    assert selected == pytest.approx(expected, abs=1.5), (
        f"云端底片帧循环不是乒乓镜像: {selected} vs {expected}"
    )
    # 关键反例：模运算下第 3 帧会回到底片 30（乒乓应为 60）
    assert abs(selected[3] - base_values[1]) < abs(selected[3] - base_values[0]), (
        f"第 3 帧应取底片 {base_values[1]}（乒乓），实际接近 {selected[3]}"
    )


# ---------------------------------------------------------------------------
# P0-1: 试播降级/补帧路径使用乒乓镜像循环
# ---------------------------------------------------------------------------
def _latentsync_service(monkeypatch, asset_dir):
    from server.tests.test_speech_drive_preview import _latentsync_service as _svc

    return _svc(monkeypatch, asset_dir)


@pytest.mark.anyio
async def test_latentsync_degraded_fallback_uses_pingpong_loop(monkeypatch, tmp_path):
    """云端灰底坏帧回退主播底片时，底片帧循环必须是乒乓镜像（用户可见的回退画面）。"""
    from server.tests.test_speech_drive_preview import _gray_placeholder_b64

    asset = _make_multi_face_asset(tmp_path, n=3)
    base_values = _base_values(3)
    svc = _latentsync_service(monkeypatch, asset)
    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000 * 2)) * 0.2).astype(np.float32)  # 50 帧
    gray_b64 = _gray_placeholder_b64()

    async def _fake_post(batch_url, headers, seg_pcm, face_imgs_b64, anchor_id):
        return [gray_b64] * 25  # 两轮全部灰底 -> 全部回退底片

    monkeypatch.setattr(svc, "_post_latentsync_batch", _fake_post)

    outcome = await svc._render_cloud_batch_latentsync(
        {"gpu_name": "NVIDIA A100", "vram_total_gb": 80.0}, asset, pcm, "anchor_pp", 50
    )
    assert len(outcome.face_frames) == 50

    selected = [_corner_mean(f) for f in outcome.face_frames]
    expected = [base_values[mirror_index(i, 3)] for i in range(50)]
    assert selected == pytest.approx(expected, abs=3.0), (
        f"降级回退帧不是乒乓镜像循环: {selected[:8]}... vs {expected[:8]}..."
    )


@pytest.mark.anyio
async def test_latentsync_padding_uses_pingpong_loop(monkeypatch, tmp_path):
    """分段渲染缺口补帧必须衔接真人底片的乒乓镜像时序，而非模运算硬跳变。"""
    from server.tests.test_speech_drive_preview import _textured_face_b64

    asset = _make_multi_face_asset(tmp_path, n=3)
    base_values = _base_values(3)
    svc = _latentsync_service(monkeypatch, asset)
    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000 * 2)) * 0.2).astype(np.float32)  # 50 帧
    good_b64 = _textured_face_b64()

    rounds = 0

    async def _fake_post(batch_url, headers, seg_pcm, face_imgs_b64, anchor_id):
        # 第 1 轮只完成 5 帧，之后节点停滞（空返回）-> 剩余 45 帧走补帧路径
        nonlocal rounds
        rounds += 1
        return [good_b64] * 5 if rounds == 1 else []

    monkeypatch.setattr(svc, "_post_latentsync_batch", _fake_post)

    outcome = await svc._render_cloud_batch_latentsync(
        {"gpu_name": "NVIDIA A100", "vram_total_gb": 80.0}, asset, pcm, "anchor_pad", 50
    )
    assert len(outcome.face_frames) == 50
    assert outcome.fallback_reason and "5/50" in outcome.fallback_reason

    # 前 5 帧为云端渲染帧；补帧 [5:50] 必须服从乒乓镜像
    selected = [_corner_mean(f) for f in outcome.face_frames[5:]]
    expected = [base_values[mirror_index(i, 3)] for i in range(5, 50)]
    assert selected == pytest.approx(expected, abs=3.0), (
        f"补帧不是乒乓镜像循环: {selected[:8]}... vs {expected[:8]}..."
    )
