# -*- coding: utf-8 -*-
"""试播台词驱动编排服务测试"""
import asyncio
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from server.core.avatar.speech_drive_preview import SidecarConnection
from server.database.models import Anchor


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


@pytest.fixture
def fake_asset(tmp_path):
    """构造最小可用资产目录 (1 张 face + 1 张 full + coords)"""
    import pickle

    import cv2

    (tmp_path / "face_imgs").mkdir()
    (tmp_path / "full_imgs").mkdir()
    cv2.imwrite(str(tmp_path / "face_imgs" / "0.jpg"), np.zeros((256, 256, 3), dtype=np.uint8))
    cv2.imwrite(str(tmp_path / "full_imgs" / "0.jpg"), np.zeros((200, 200, 3), dtype=np.uint8))
    with open(tmp_path / "coords.pkl", "wb") as f:
        pickle.dump([(0, 100, 0, 100)], f)
    return tmp_path


@pytest.fixture
def no_gpu(monkeypatch):
    """强制 evaluate_compute 返回本地不可用、云端未配置"""

    async def _fake_evaluate(*args, **kwargs):
        from server.core.hardware.gpu_capability import ComputePlan

        return ComputePlan(
            feature_name="test",
            required_vram_gb=8,
            use_cloud=False,
            can_execute=False,
            is_low_spec_local=True,
            has_cloud_gpu=False,
            local_gpu={"gpu_name": "GT 710", "vram_total_gb": 1.0, "cuda_available": False},
            cloud_gpu=None,
            alert_type="insufficient_hardware",
            user_message="test",
            recommended_driver="procedural",
        )

    from server.core.hardware import gpu_capability as gc

    monkeypatch.setattr(gc, "evaluate_compute", _fake_evaluate)


def test_render_local_sync_produces_frames(monkeypatch, fake_asset):
    from server.core.avatar.speech_drive_preview import _render_local_sync

    class _FakeRenderer:
        def crop_face_256(self, full, coord):
            import cv2

            return cv2.resize(full, (256, 256))

        def render_lip_frame(self, full, idx, pcm, mouth_open, return_face=False):
            import cv2

            face = cv2.resize(full, (256, 256))
            return (full, face) if return_face else full

    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000)) * 0.2).astype(np.float32)  # 1s
    full_imgs = [cv2.imread(str(fake_asset / "full_imgs" / "0.jpg"))]
    renderer = _FakeRenderer()
    outcome = _render_local_sync(renderer, pcm, 25, full_imgs)
    assert len(outcome.face_frames) == 25 and len(outcome.full_frames) == 25
    assert all(f.shape[:2] == (256, 256) for f in outcome.face_frames)
    assert len(outcome.timings_ms) == 25 and all(t >= 0.0 for t in outcome.timings_ms)


def _silence_mp3() -> bytes:
    """生成一段可被 soundfile 解码的最小 WAV（测试统一用 wav 容器）"""
    import io

    import soundfile as sf  # noqa: PLC0415  (测试环境依赖)

    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(16000 * 1.0), dtype=np.float32), 16000, format="WAV")
    return buf.getvalue()


@pytest.mark.anyio
async def test_service_blocks_without_adequate_gpu(monkeypatch, fake_asset, no_gpu):
    """硬件门禁 (用户规约)：无 >=8GB 本地 CUDA 显卡且无云端 GPU 时，直接禁止试播，绝无降级"""
    from server.core.avatar.speech_drive_preview import (
        PREVIEW_SESSION_ROOT,
        PreviewHardwareError,
        get_speech_drive_preview_service,
    )

    service = get_speech_drive_preview_service()
    service._sessions_root = fake_asset / "sessions"
    audio = _silence_mp3()
    try:
        with pytest.raises(PreviewHardwareError) as exc:
            await service.run("anchor_test", fake_asset, audio)
        assert "硬件不足" in str(exc.value)
    finally:
        service._sessions_root = PREVIEW_SESSION_ROOT


def _make_plan(local_vram=0.0, cuda=False, cloud=None, use_cloud=False, has_cloud=False):
    from server.core.hardware.gpu_capability import ComputePlan

    return ComputePlan(
        feature_name="test",
        required_vram_gb=8.0,
        use_cloud=use_cloud,
        can_execute=True,
        is_low_spec_local=not cuda,
        has_cloud_gpu=has_cloud,
        local_gpu={"gpu_name": "Test GPU", "vram_total_gb": local_vram, "cuda_available": cuda},
        cloud_gpu=cloud,
        alert_type="none",
        user_message="",
        recommended_driver="cloud_sidecar",
    )


def _make_outcome(n=3):
    from server.core.avatar.speech_drive_preview import RenderOutcome

    outcome = RenderOutcome()
    outcome.device = "NVIDIA A100-SXM4-80GB"
    outcome.full_frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in range(n)]
    outcome.face_frames = [np.zeros((256, 256, 3), dtype=np.uint8) for _ in range(n)]
    outcome.timings_ms = [10.0] * n
    return outcome


@pytest.mark.anyio
async def test_dispatch_rejects_cloud_gpu_below_8gb(fake_asset):
    """云端 GPU 显存 <8GB 时必须禁止试播 (硬性门槛 8GB)"""
    from server.core.avatar.speech_drive_preview import (
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 4.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()
    with pytest.raises(PreviewHardwareError) as exc:
        await service._dispatch_render(
            _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
            fake_asset, np.zeros(16000, dtype=np.float32), 25,
        )
    assert "硬件不足" in str(exc.value)


@pytest.mark.anyio
async def test_dispatch_rejects_cloud_gpu_with_unknown_vram(fake_asset):
    """云端节点显存未探明 (vram_total_gb=0) 时必须禁止试播。

    历史缺陷：曾按「只要可连通就当作 8GB 显存」放行，导致纯 CPU 节点被当成
    真实 GPU 节点进入渲染分支，违反「显存 >=8GB 才允许试播」的硬性门槛。
    显存必须来自实测探活 (gpu_capability.parse_device_string)，不得伪造。
    """
    from server.core.avatar.speech_drive_preview import (
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 0.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _should_not_run(cloud_gpu, asset_dir, pcm, n_frames):
        raise AssertionError("显存未探明的云端节点不得进入真实渲染分支")

    service._render_cloud = _should_not_run  # noqa: SLF001
    with pytest.raises(PreviewHardwareError) as exc:
        await service._dispatch_render(
            _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
            fake_asset, np.zeros(16000, dtype=np.float32), 25,
        )
    assert "硬件不足" in str(exc.value)


@pytest.mark.anyio
async def test_dispatch_cloud_success(fake_asset):
    """云端 GPU 达标 (80GB A100) 且渲染成功 → neural_cloud_sidecar，无回退原因"""
    from server.core.avatar.speech_drive_preview import (
        ENGINE_CLOUD,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 80.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _fake_render_cloud(cloud_gpu, asset_dir, pcm, n_frames):
        return _make_outcome()

    service._render_cloud = _fake_render_cloud  # noqa: SLF001
    outcome, engine, mode, fallback_reason = await service._dispatch_render(
        _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
        fake_asset, np.zeros(16000, dtype=np.float32), 25,
    )
    assert engine == ENGINE_CLOUD and mode == "cloud"
    assert fallback_reason is None
    assert len(outcome.full_frames) == 3


@pytest.mark.anyio
async def test_dispatch_cloud_failure_blocks_without_local_gpu(fake_asset):
    """云端渲染失败且本地无 >=8GB 显卡 → 直接禁止试播并如实给出云端失败原因"""
    from server.core.avatar.speech_drive_preview import (
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 80.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _boom(cloud_gpu, asset_dir, pcm, n_frames):
        raise RuntimeError("远程 sidecar 必须配置鉴权 token")

    service._render_cloud = _boom  # noqa: SLF001
    with pytest.raises(PreviewHardwareError) as exc:
        await service._dispatch_render(
            _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
            fake_asset, np.zeros(16000, dtype=np.float32), 25,
        )
    assert "鉴权 token" in str(exc.value)


@pytest.mark.anyio
async def test_dispatch_cloud_failure_falls_to_local_gpu(fake_asset):
    """云端失败 → 本地 >=8GB CUDA 显卡接手真实神经推理 (同为真实渲染，非降级)"""
    from server.core.avatar.speech_drive_preview import (
        ENGINE_LOCAL,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 80.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _boom(cloud_gpu, asset_dir, pcm, n_frames):
        raise RuntimeError("connection refused")

    async def _fake_render_local(asset_dir, pcm, n_frames):
        return _make_outcome()

    service._render_cloud = _boom  # noqa: SLF001
    service._render_local = _fake_render_local  # noqa: SLF001
    outcome, engine, mode, fallback_reason = await service._dispatch_render(
        _make_plan(local_vram=12.0, cuda=True, cloud=cloud, use_cloud=True, has_cloud=True),
        fake_asset, np.zeros(16000, dtype=np.float32), 25,
    )
    assert engine == ENGINE_LOCAL and mode == "local"
    assert fallback_reason == "sidecar_unreachable"


@pytest.mark.anyio
async def test_dispatch_local_gpu_success(fake_asset):
    """仅本地 >=8GB CUDA 显卡可用 → 本地 ONNX GPU 真实推理"""
    from server.core.avatar.speech_drive_preview import (
        ENGINE_LOCAL,
        SpeechDrivePreviewService,
    )

    service = SpeechDrivePreviewService()

    async def _fake_render_local(asset_dir, pcm, n_frames):
        return _make_outcome()

    service._render_local = _fake_render_local  # noqa: SLF001
    outcome, engine, mode, fallback_reason = await service._dispatch_render(
        _make_plan(local_vram=12.0, cuda=True),
        fake_asset, np.zeros(16000, dtype=np.float32), 25,
    )
    assert engine == ENGINE_LOCAL and mode == "local"
    assert fallback_reason is None


# ---------------------------------------------------------------------------
# Task 5: HTTP 端点契约
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app)


@pytest.fixture
def anchor_with_asset(client, fake_asset):
    """通过 /anchors/create 注册一个主播，并把资产目录指向临时 fake_asset"""
    import asyncio

    from server.database.db import AsyncSessionLocal

    create_res = client.post(
        "/api/v1/anchors/create",
        data={"name": "演示主播", "voice_id": "voice_default_female", "remark": "试播演示"},
    )
    assert create_res.status_code == 200
    anchor_id = create_res.json()["data"]["id"]

    async def _point_asset():
        async with AsyncSessionLocal() as db:
            anchor = await db.get(Anchor, anchor_id)
            anchor.avatar_asset_dir = str(fake_asset)
            await db.commit()

    asyncio.run(_point_asset())
    return anchor_id


def test_endpoint_rejects_missing_anchor(client):
    res = client.post("/api/v1/anchors/no_such_anchor/avatar/preview-speech-drive", json={"text": "你好"})
    assert res.status_code == 404


def test_endpoint_rejects_empty_text(client, anchor_with_asset):
    res = client.post(
        f"/api/v1/anchors/{anchor_with_asset}/avatar/preview-speech-drive",
        json={"text": "   "},
    )
    assert res.status_code == 422


def test_endpoint_rejects_too_long_text(client, anchor_with_asset):
    res = client.post(
        f"/api/v1/anchors/{anchor_with_asset}/avatar/preview-speech-drive",
        json={"text": "字" * 501},
    )
    assert res.status_code == 422


def test_endpoint_rejects_asset_not_ready(client):
    """资产目录缺失 face_imgs 时应返回 409"""
    create_res = client.post(
        "/api/v1/anchors/create",
        data={"name": "无资产主播", "voice_id": "voice_default_female"},
    )
    assert create_res.status_code == 200
    anchor_id = create_res.json()["data"]["id"]
    res = client.post(
        f"/api/v1/anchors/{anchor_id}/avatar/preview-speech-drive",
        json={"text": "你好"},
    )
    assert res.status_code == 409


def test_endpoint_blocks_insufficient_hardware(client, anchor_with_asset, monkeypatch, no_gpu):
    """硬件门禁 (用户规约)：无 >=8GB 本地显卡且无云端 GPU 时，端点直接 424 禁止试播"""
    import server.core.audio.tts_preview_service as tps

    async def _fake_synth(params):
        return _silence_mp3(), "audio/wav"

    monkeypatch.setattr(tps, "synthesize_preview_audio", _fake_synth)

    res = client.post(
        f"/api/v1/anchors/{anchor_with_asset}/avatar/preview-speech-drive",
        json={"text": "大家好，欢迎来到直播间"},
    )
    assert res.status_code == 424, res.text
    assert "硬件不足" in res.json()["detail"]


def test_preview_session_frame_rejects_path_traversal(client, anchor_with_asset):
    """静态帧路由对 ../ 与非法 session_id 返回 404"""
    base = f"/api/v1/anchors/{anchor_with_asset}/preview-sessions"
    assert client.get(f"{base}/00000000000000000000/face/0.jpg").status_code == 404
    assert client.get(f"{base}/..%2F..%2Fface/0.jpg").status_code in (404, 422)
    assert client.get(f"{base}/00000000000000000000/audio.mp3").status_code == 404


# ---------------------------------------------------------------------------
# Task 6: 云端 Sidecar 批量推理
# ---------------------------------------------------------------------------
def _fake_cloud_info():
    from server.core.hardware.gpu_capability import CloudGpuInfo

    return CloudGpuInfo(
        configured=True,
        provider_name="sidecar_v3",
        adapter="sidecar_v3",
        base_url="ws://127.0.0.1:8890/ws/render-v3",
        is_active=True,
        is_reachable=True,
        api_key="",
        gpu_name="NVIDIA RTX 4090",
        vram_total_gb=24.0,
    )


class _FakeCloudDriver:
    """模拟 sidecar 驱动：feed_audio_frames 时发布 2 帧并完成"""

    def __init__(self):
        self.started = False
        self.stopped = False
        self.published: list = []
        self._selected_descriptor = {"model_version": "musetalk-0.1", "device": "NVIDIA RTX 4090"}
        self._timeline_task = None

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def feed_audio_frames(self, frames):
        import cv2

        for i in range(2):
            ok, buf = cv2.imencode(".jpg", np.full((64, 64, 3), 30 + i, dtype=np.uint8))
            await self._publish_video_frame(buf.tobytes(), None)

    async def _publish_video_frame(self, jpeg, owner):
        self.published.append(jpeg)


@pytest.mark.anyio
async def test_render_cloud_collects_frames(monkeypatch, fake_asset):
    """mock 驱动：验证云端会话收集 JPEG 帧并按序号排序"""
    from server.core.avatar.speech_drive_preview import (
        SpeechDrivePreviewService,
        _build_audio_frame_batch,
    )

    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000)) * 0.2).astype(np.float32)
    frames = _build_audio_frame_batch(pcm)
    assert frames[0].is_first and frames[-1].is_final
    assert frames[-1].pts_samples + frames[-1].duration_samples == len(pcm)

    service = SpeechDrivePreviewService()

    driver = _FakeCloudDriver()

    async def _fake_resolve(cloud_gpu):
        return SidecarConnection(
            node_url=cloud_gpu.base_url,
            auth_token="",
            canonical={"backend_id": "musetalk", "avatar_id": "default"},
            model_name="auto",
        )

    def _fake_build(conn):
        return driver

    monkeypatch.setattr(service, "_resolve_sidecar_connection", _fake_resolve)
    monkeypatch.setattr(
        "server.core.avatar.speech_drive_preview._build_sidecar_driver", _fake_build
    )
    async def _noop_ensure(conn, asset_dir):
        pass
    monkeypatch.setattr(
        "server.core.avatar.speech_drive_preview._ensure_cloud_assets", _noop_ensure
    )

    cloud = _fake_cloud_info()
    outcome = await service._render_cloud(cloud, fake_asset, pcm, 2)
    assert outcome is not None
    assert len(outcome.full_frames) == 2
    assert all(f.shape[0] == 64 for f in outcome.full_frames)
    assert len(outcome.face_frames) == 2
    assert outcome.device == "NVIDIA RTX 4090"
    assert driver.started and driver.stopped


@pytest.mark.anyio
async def test_render_cloud_failure_raises_real_reason(monkeypatch, fake_asset):
    """握手失败时如实抛出真实原因 (如缺少鉴权 token)，交由上层门禁透传给用户"""
    from server.core.avatar.speech_drive_preview import (
        SpeechDrivePreviewService,
    )

    class _BoomDriver:
        async def start(self):
            raise RuntimeError("connection refused")

        async def stop(self):
            pass

    service = SpeechDrivePreviewService()

    async def _fake_resolve(cloud_gpu):
        return SidecarConnection(
            node_url=cloud_gpu.base_url,
            auth_token="",
            canonical={"backend_id": "musetalk"},
            model_name="auto",
        )

    def _fake_build(conn):
        return _BoomDriver()

    monkeypatch.setattr(service, "_resolve_sidecar_connection", _fake_resolve)
    monkeypatch.setattr(
        "server.core.avatar.speech_drive_preview._build_sidecar_driver", _fake_build
    )
    async def _noop_ensure(conn, asset_dir):
        pass
    monkeypatch.setattr(
        "server.core.avatar.speech_drive_preview._ensure_cloud_assets", _noop_ensure
    )

    with pytest.raises(RuntimeError, match="connection refused"):
        await service._render_cloud(_fake_cloud_info(), fake_asset, np.zeros(1600, dtype=np.float32), 1)

# ---------------------------------------------------------------------------
# Task 7: 资产同步
# ---------------------------------------------------------------------------
def test_sample_cloud_asset_zip(tmp_path):
    """_sample_cloud_asset_zip 生成合法 zip，含 face/full imgs + coords.pkl + meta.json"""
    import io
    import json
    import pickle
    from server.core.avatar.speech_drive_preview import _sample_cloud_asset_zip
    import zipfile

    (tmp_path / "face_imgs").mkdir()
    (tmp_path / "full_imgs").mkdir()
    cv2.imwrite(str(tmp_path / "face_imgs" / "0.jpg"), np.zeros((256, 256, 3), dtype=np.uint8))
    cv2.imwrite(str(tmp_path / "face_imgs" / "1.jpg"), np.ones((256, 256, 3), dtype=np.uint8) * 50)
    cv2.imwrite(str(tmp_path / "full_imgs" / "0.jpg"), np.zeros((200, 200, 3), dtype=np.uint8))
    cv2.imwrite(str(tmp_path / "full_imgs" / "1.jpg"), np.ones((200, 200, 3), dtype=np.uint8) * 50)
    with open(tmp_path / "coords.pkl", "wb") as f:
        pickle.dump([(0, 100, 0, 100), (10, 110, 10, 110)], f)

    zip_bytes, sha = _sample_cloud_asset_zip(tmp_path, max_frames=2)
    assert zip_bytes and sha
    assert len(sha) == 64
    bio = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(bio, "r") as zf:
        names = zf.namelist()
        assert "face_imgs/0.jpg" in names and "face_imgs/1.jpg" in names
        assert "full_imgs/0.jpg" in names and "full_imgs/1.jpg" in names
        assert "coords.pkl" in names
        assert "meta.json" in names
        meta = json.loads(zf.read("meta.json"))
        assert meta["sample_count"] == 2


def test_sample_cloud_asset_zip_max_frames(tmp_path):
    """_sample_cloud_asset_zip 尊重 max_frames 上限"""
    import io
    from server.core.avatar.speech_drive_preview import _sample_cloud_asset_zip
    import zipfile

    (tmp_path / "face_imgs").mkdir()
    (tmp_path / "full_imgs").mkdir()
    for i in range(5):
        cv2.imwrite(str(tmp_path / "face_imgs" / f"{i}.jpg"), np.zeros((256, 256, 3), dtype=np.uint8))
        cv2.imwrite(str(tmp_path / "full_imgs" / f"{i}.jpg"), np.zeros((200, 200, 3), dtype=np.uint8))

    zip_bytes, _ = _sample_cloud_asset_zip(tmp_path, max_frames=3)
    bio = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(bio, "r") as zf:
        face_names = [n for n in zf.namelist() if n.startswith("face_imgs/")]
        assert len(face_names) == 3


@pytest.mark.anyio
async def test_ensure_cloud_assets_404_raises_actionable(monkeypatch, fake_asset):
    """所有资产端点都不存在 (极旧 sidecar) 时给出可操作错误 (提示旧版部署需重新部署)"""
    from server.core.avatar.speech_drive_preview import _ensure_cloud_assets, SidecarConnection
    import urllib.error
    import urllib.request

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self, code):
            self.code = code
        def read(self):
            return b""

    def _fake_urlopen(req, timeout=None):
        raise FakeHTTPError(404)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    with pytest.raises(RuntimeError, match="旧版部署"):
        await _ensure_cloud_assets(conn, fake_asset)


class _FakeSidecar:
    """进程内模拟云端 sidecar：GET 摘要 + 分块上传/提交 + 旧版整包接口，供单测验证客户端协议。"""

    BASE = "https://fake-sidecar.example.com/assets/test_anchor"

    def __init__(self, *, chunked=True, stored_digest=None, fail_first_n_posts=0):
        self.chunked = chunked
        self.stored_digest = stored_digest     # GET 返回摘要；None 表示资产不存在 (404)
        self.fail_first_n_posts = fail_first_n_posts
        self.calls: list = []                  # (method, url, timeout)
        self.chunks: dict = {}                 # upload_id -> {index: bytes}
        self.chunk_posts: list = []            # (upload_id, index) 按发送顺序
        self.responses: list = []              # 所有返回的响应对象 (校验是否被读取/关闭)
        self.stored_zip: bytes | None = None
        self.commit_count = 0

    def urlopen(self, req, timeout=None):
        import base64
        import hashlib
        import json
        import urllib.error

        method, url = req.get_method(), req.get_full_url()
        ua = req.headers.get("User-agent", "")
        self.calls.append((method, url, timeout))
        # 所有资产同步请求必须携带应用标识 UA，规避 Cloudflare Bot Fight Mode
        # 对 Python-urllib 默认签名的封禁 (403 error 1010)
        assert ua == "AI-LiveStream-Agent/1.0", f"资产请求 UA 必须为应用标识，实际: {ua!r}"
        class _Resp:
            def __init__(self, status, body):
                self.status, self._body = status, body
                self.read_calls = 0
                self.close_calls = 0

            def read(self):
                self.read_calls += 1
                return self._body

            def close(self):
                self.close_calls += 1

        class _Err(urllib.error.HTTPError):
            def __init__(self, code, body=b""):
                self.code, self._body = code, body
                self.read_calls = 0
                self.close_calls = 0

            def read(self):
                self.read_calls += 1
                return self._body

            def close(self):
                self.close_calls += 1

        def _track(resp):
            self.responses.append(resp)
            return resp

        if method == "GET":
            if self.stored_digest is None:
                raise _Err(404)
            return _track(_Resp(200, json.dumps({"sha256": self.stored_digest}).encode()))

        if self.fail_first_n_posts > 0:
            self.fail_first_n_posts -= 1
            raise urllib.error.URLError("The write operation timed out")

        body = json.loads(req.data.decode("utf-8"))
        if url.endswith("/chunks"):
            if not self.chunked:
                raise _Err(404)
            rec = self.chunks.setdefault(body["upload_id"], {})
            rec[body["index"]] = base64.b64decode(body["data"])
            self.chunk_posts.append((body["upload_id"], body["index"]))
            # 与真实服务端 (intern_gpu.md upload_asset_chunk) 同口径：回传已收块数
            return _track(_Resp(200, json.dumps({
                "avatar_id": "test_anchor", "index": body["index"],
                "received": len(rec), "total": body["total"],
            }).encode()))
        if url.endswith("/commit"):
            if not self.chunked:
                raise _Err(404)
            parts = self.chunks.get(body["upload_id"], {})
            if len(parts) != body["total"]:
                raise _Err(400, b"chunks incomplete")
            assembled = b"".join(parts[i] for i in range(body["total"]))
            if hashlib.sha256(assembled).hexdigest() != body["sha256"]:
                raise _Err(400, b"sha256 mismatch")
            self.stored_zip = assembled
            self.commit_count += 1
            return _track(_Resp(200, b'{"face_count": 1}'))
        if url == self.BASE:  # 旧版整包上传
            self.stored_zip = base64.b64decode(body["zip_base64"])
            return _track(_Resp(200, b'{"face_count": 1}'))
        raise _Err(404)


@pytest.fixture
def small_chunks(monkeypatch):
    """把分块粒度调小，使最小资产也能产生多块，从而覆盖逐块重试/断点续传路径。"""
    import server.core.avatar.speech_drive_preview as sdp

    monkeypatch.setattr(sdp, "ASSET_UPLOAD_CHUNK_BYTES", 512)
    return 512


@pytest.mark.anyio
async def test_upload_chunked_reads_and_closes_every_response(monkeypatch, fake_asset, small_chunks):
    """每个 HTTP 响应都必须被读取并关闭。

    历史缺陷：分块上传直接丢弃 urlopen 返回值，既不 read() 也不 close()，
    导致 socket / TLS 会话在 10+ 次串行请求中持续泄漏，服务端侧还可能收到
    broken pipe。必须逐个消费并关闭响应体。
    """
    import urllib.request

    from server.core.avatar.speech_drive_preview import _ensure_cloud_assets, SidecarConnection

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    server = _FakeSidecar(stored_digest=None)
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    assert len(server.chunk_posts) >= 3, "本用例需要多块才能覆盖逐块响应处理"
    assert server.responses, "应至少产生一个响应"
    unread = [r for r in server.responses if r.read_calls == 0]
    unclosed = [r for r in server.responses if r.close_calls == 0]
    assert not unread, f"{len(unread)} 个响应体未被读取"
    assert not unclosed, f"{len(unclosed)} 个响应未被关闭"


@pytest.mark.anyio
async def test_upload_chunked_resumes_from_server_reported_count(monkeypatch, fake_asset, small_chunks):
    """重试时只补传服务端尚缺的分块，不从 chunk 0 重来。

    历史缺陷：每次调用生成全新 upload_id，任一块失败则已传分块全部作废；
    弱网隧道上 9.8MB / 10 块几乎永远无法从头传完。upload_id 必须由
    (avatar_id, zip sha256) 确定化，并据服务端回传的 received 断点续传。
    """
    import urllib.request

    from server.core.avatar.speech_drive_preview import (
        ASSET_UPLOAD_CHUNK_BYTES,
        _cloud_upload_id,
        _ensure_cloud_assets,
        _sample_cloud_asset_zip,
        SidecarConnection,
    )

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    zip_bytes, zip_sha = _sample_cloud_asset_zip(fake_asset)
    total = (len(zip_bytes) + ASSET_UPLOAD_CHUNK_BYTES - 1) // ASSET_UPLOAD_CHUNK_BYTES
    assert total >= 3, f"本用例需要至少 3 块才有断点续传意义，实际 {total}"

    # 预置：上一次失败前服务端已收到 chunk 0 与 1
    server = _FakeSidecar(stored_digest=None)
    upload_id = _cloud_upload_id("test_anchor", zip_sha)
    server.chunks[upload_id] = {
        0: zip_bytes[0:ASSET_UPLOAD_CHUNK_BYTES],
        1: zip_bytes[ASSET_UPLOAD_CHUNK_BYTES:2 * ASSET_UPLOAD_CHUNK_BYTES],
    }
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    sent = [idx for uid, idx in server.chunk_posts if uid == upload_id]
    # chunk 0 必须重发一次以探知服务端已收块数 (幂等覆盖)，此后只补缺失的块
    assert sent == [0] + list(range(2, total)), f"应只补传缺失块，实际发送顺序 {sent}"
    assert server.stored_zip == zip_bytes, "断点续传后组装结果必须与本地采样包逐字节一致"
    assert server.commit_count == 1


@pytest.mark.anyio
async def test_upload_chunked_backoff_grows_and_is_capped(monkeypatch, fake_asset, small_chunks):
    """单块重试必须使用指数退避 + 抖动，而非固定短退避。

    历史缺陷：退避固定为 1.5s / 3s。实测隧道被 Cloudflare 断链后往往需要数十秒
    才恢复，固定短退避会在链路尚未恢复时耗尽全部 3 次重试，直接把可恢复的
    抖动判成永久失败。退避须指数增长、抖动化并设上限。
    """
    import asyncio as _asyncio
    import urllib.request

    import server.core.avatar.speech_drive_preview as sdp
    from server.core.avatar.speech_drive_preview import _ensure_cloud_assets, SidecarConnection

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    server = _FakeSidecar(stored_digest=None, fail_first_n_posts=99)  # 持续失败以观察退避序列
    slept: list[float] = []
    _orig_sleep = _asyncio.sleep

    async def _fake_sleep(delay, *_a, **_k):
        slept.append(float(delay))
        return await _orig_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    with pytest.raises(RuntimeError, match="多次重试后仍无法连接"):
        await _ensure_cloud_assets(conn, fake_asset)

    delays = slept[: sdp.CHUNK_UPLOAD_ATTEMPTS - 1]
    assert len(delays) == sdp.CHUNK_UPLOAD_ATTEMPTS - 1, f"应有 {sdp.CHUNK_UPLOAD_ATTEMPTS - 1} 次退避，实际 {delays}"

    lo, hi = sdp.CHUNK_UPLOAD_BACKOFF_JITTER
    for i, delay in enumerate(delays):
        base = min(sdp.CHUNK_UPLOAD_BACKOFF_CAP_SEC, sdp.CHUNK_UPLOAD_BACKOFF_BASE_SEC * (2 ** i))
        assert base * lo <= delay <= min(sdp.CHUNK_UPLOAD_BACKOFF_CAP_SEC * hi, base * hi) + 1e-6, (
            f"第 {i + 1} 次退避 {delay:.2f}s 超出 [{base * lo:.2f}, {base * hi:.2f}] 区间"
        )

    # 指数性：每次退避的下界严格大于上一次 (旧实现固定 1.5s/3.0s，下界恒为 0.75/1.5)
    lower_bounds = [sdp.CHUNK_UPLOAD_BACKOFF_BASE_SEC * (2 ** i) * lo for i in range(len(delays))]
    assert lower_bounds[1] > lower_bounds[0], f"退避下界未指数增长: {lower_bounds}"
    # 旧实现首退避恒为 1.5s；新实现首退避下界须高于它，才谈得上「给足恢复窗口」
    assert lower_bounds[0] > 1.5, f"首退避下界应高于旧实现的 1.5s，实际 {lower_bounds[0]:.2f}s"


@pytest.mark.anyio
async def test_dispatch_reports_asset_sync_failure_as_bandwidth_issue(fake_asset):
    """资产同步阶段的失败必须被归类为「带宽/链路」问题，而不是误导用户去查鉴权。

    历史缺陷：资产上传失败被笼统包装成「云端 GPU 渲染失败」，并统一附上
    「请检查云端 GPU 节点状态与鉴权配置」。实测 9.8MB 资产在 ~6KB/s 隧道上
    传 20 分钟仍失败，用户却被告警去检查鉴权配置，方向完全错误。
    """
    from server.core.avatar.speech_drive_preview import (
        CloudAssetSyncError,
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 15.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _fail_on_asset_sync(cloud_gpu, asset_dir, pcm, n_frames):
        raise CloudAssetSyncError("资产上传失败: chunk 5/10 多次重试后仍无法连接")

    service._render_cloud = _fail_on_asset_sync  # noqa: SLF001
    with pytest.raises(PreviewHardwareError) as exc:
        await service._dispatch_render(
            _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
            fake_asset, np.zeros(16000, dtype=np.float32), 25,
        )
    msg = str(exc.value)
    assert "资产" in msg, f"应指明是资产同步阶段失败: {msg}"
    assert "带宽" in msg or "链路" in msg, f"应指明根因方向是带宽/链路: {msg}"
    assert "鉴权配置" not in msg, f"带宽问题不应误导用户去查鉴权配置: {msg}"


@pytest.mark.anyio
async def test_ensure_cloud_assets_raises_asset_sync_error_on_chunk_failure(monkeypatch, fake_asset, small_chunks):
    """分块上传失败必须抛 CloudAssetSyncError，供上层按阶段归类，而非混作渲染失败。"""
    import asyncio
    import urllib.request

    from server.core.avatar.speech_drive_preview import (
        CloudAssetSyncError,
        _ensure_cloud_assets,
        SidecarConnection,
    )

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    server = _FakeSidecar(stored_digest=None, fail_first_n_posts=99)
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    with pytest.raises(CloudAssetSyncError, match="多次重试后仍无法连接"):
        await _ensure_cloud_assets(conn, fake_asset)


@pytest.mark.anyio
async def test_dispatch_reports_node_backend_not_ready_not_auth(fake_asset):
    """节点侧后端未就绪 (render_rejected) 不得被归类为鉴权问题。

    真实故障：云端节点握手与 auth_ok 均声明 available=true，却在 render_open 时
    回 `descriptor.available 必须为 true`——说明节点自身的后端就绪探测为 false
    (典型是 onnxruntime 落到 CPUExecutionProvider)。此时鉴权已成功，
    提示用户「检查鉴权配置」会把排查方向完全带偏。
    """
    from server.adapters.media.neural_sidecar_driver import SidecarBackendNotReadyError
    from server.core.avatar.speech_drive_preview import (
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 15.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _node_not_ready(cloud_gpu, asset_dir, pcm, n_frames):
        raise SidecarBackendNotReadyError(
            "sidecar 拒绝 render_open: descriptor.available 必须为 true"
        )

    service._render_cloud = _node_not_ready  # noqa: SLF001
    with pytest.raises(PreviewHardwareError) as exc:
        await service._dispatch_render(
            _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
            fake_asset, np.zeros(16000, dtype=np.float32), 25,
        )
    msg = str(exc.value)
    assert "鉴权配置" not in msg, f"节点未就绪不应提示检查鉴权: {msg}"
    assert "就绪" in msg or "未就绪" in msg, f"应指明是节点侧后端就绪问题: {msg}"


@pytest.mark.anyio
async def test_render_rejected_readiness_reason_raises_backend_not_ready():
    """render_rejected 的就绪类原因必须抛 SidecarBackendNotReadyError，供上层精确归类。"""
    from server.adapters.media.neural_sidecar_driver import (
        NeuralSidecarMediaDriver,
        SidecarBackendNotReadyError,
    )

    driver = NeuralSidecarMediaDriver.__new__(NeuralSidecarMediaDriver)
    driver._selected_descriptor = {"id": "cloud_sidecar", "avatar_id": "default"}
    payload = {
        "event": "render_rejected",
        "request_id": "req_1",
        "reason": "descriptor.available 必须为 true",
    }
    with pytest.raises(SidecarBackendNotReadyError) as exc:
        driver._validate_render_accepted(payload, "req_1")
    assert "鉴权" not in str(exc.value)


def test_auth_rejection_is_not_classified_as_backend_not_ready():
    """鉴权类拒绝 (auth 失败) 不得被误判为「后端未就绪」，二者根因完全不同。"""
    from server.adapters.media.neural_sidecar_driver import (
        NeuralSidecarMediaDriver,
        SidecarBackendNotReadyError,
    )

    driver = NeuralSidecarMediaDriver.__new__(NeuralSidecarMediaDriver)
    driver._selected_descriptor = {"id": "cloud_sidecar", "avatar_id": "default"}
    payload = {"event": "render_rejected", "request_id": "req_1", "reason": "鉴权失败：Token 不匹配"}
    with pytest.raises(RuntimeError) as exc:
        driver._validate_render_accepted(payload, "req_1")
    assert not isinstance(exc.value, SidecarBackendNotReadyError), "鉴权失败不得归类为后端未就绪"


def _driver(require_neural: bool = True):
    from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver

    return NeuralSidecarMediaDriver(
        node_url="wss://gpu.example.com/ws/render-v3",
        auth_token="t",
        backend_id="auto",
        avatar_id="default",
        require_neural_lipsync=require_neural,
    )


def _strict_caps(**overrides) -> dict:
    """满足 require_neural_lipsync 全部前置能力声明的最小 capabilities，再叠加覆盖项。"""
    caps = {
        "renderer_available": True,
        "neural_lipsync": True,
        "strict_completion": True,
        "streaming_video": True,
        "supports_cancel_ack": True,
        "supports_credit": True,
        "supports_render_started": True,
        "supports_sample_pts": True,
        "cancel_threadsafe": True,
        "cancel_quiesces": True,
        "input_formats": [{"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1, "sample_width": 2}],
    }
    caps.update(overrides)
    return caps


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param({"renderer_available": False, "neural_lipsync": False}, id="renderer_unavailable"),
        pytest.param({"render_backends": []}, id="no_backends"),
        pytest.param({"render_backends": None}, id="backends_not_list"),
        pytest.param(
            {"render_backends": [
                {"id": "cloud_sidecar", "available": False, "neural": False,
                 "warmed": True, "license_approved": True}
            ]},
            id="backend_not_available",
        ),
        pytest.param({"neural_lipsync": False}, id="neural_lipsync_off"),
    ],
)
def test_capability_level_readiness_failure_is_backend_not_ready(capabilities):
    """节点诚实报「不可用」时，客户端必须归类为节点未就绪，而不是笼统失败。

    真实事故：节点改为诚实反映状态后，握手 capabilities 出现
    renderer_available=false，客户端抛 RuntimeError("renderer_available 必须严格为
    true")，上层退化成「检查鉴权配置」——但鉴权其实完全正常，方向被带偏。
    """
    from server.adapters.media.neural_sidecar_driver import SidecarBackendNotReadyError

    with pytest.raises(SidecarBackendNotReadyError):
        _driver()._select_renderer_descriptor(_strict_caps(**capabilities))  # noqa: SLF001


def test_neural_lipsync_false_under_strict_requirement_is_backend_not_ready():
    """require_neural_lipsync 下 neural_lipsync=false 属节点能力不足，非鉴权问题。"""
    from server.adapters.media.neural_sidecar_driver import SidecarBackendNotReadyError

    caps = {"renderer_available": True, "neural_lipsync": False, "render_backends": [
        {"id": "cloud_sidecar", "available": True, "neural": True, "warmed": True, "license_approved": True}
    ]}
    with pytest.raises(SidecarBackendNotReadyError):
        _driver()._select_renderer_descriptor(caps)  # noqa: SLF001


def test_readiness_marker_matcher_covers_capability_wording():
    """就绪类文案的各种措辞都必须被识别（节点文案会随版本演进）。"""
    from server.adapters.media.neural_sidecar_driver import SidecarBackendNotReadyError

    for reason in (
        "sidecar renderer_available 必须严格为 true",
        "sidecar neural_lipsync 必须严格为 true",
        "backend descriptor available 必须严格为 true",
        "sidecar 缺少 render_backends descriptor",
        "sidecar 没有合格的 neural backend",
        "节点后端未就绪",
    ):
        assert SidecarBackendNotReadyError.matches_reason(reason), f"未识别为就绪类: {reason}"

    for reason in ("鉴权失败：Token 不匹配", "auth failed", "unauthorized", "403 forbidden"):
        assert not SidecarBackendNotReadyError.matches_reason(reason), f"误判为就绪类: {reason}"


@pytest.mark.anyio
async def test_dispatch_capability_rejection_not_blamed_on_auth(fake_asset):
    """能力级拒绝在预览层也必须走「节点未就绪」提示，不得出现鉴权字样。"""
    from server.adapters.media.neural_sidecar_driver import SidecarBackendNotReadyError
    from server.core.avatar.speech_drive_preview import (
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )

    cloud = {"is_reachable": True, "vram_total_gb": 15.0, "base_url": "wss://gpu.example.com/ws/render-v3"}
    service = SpeechDrivePreviewService()

    async def _cap_rejected(cloud_gpu, asset_dir, pcm, n_frames):
        raise SidecarBackendNotReadyError("sidecar renderer_available 必须严格为 true")

    service._render_cloud = _cap_rejected  # noqa: SLF001
    with pytest.raises(PreviewHardwareError) as exc:
        await service._dispatch_render(
            _make_plan(cloud=cloud, use_cloud=True, has_cloud=True),
            fake_asset, np.zeros(16000, dtype=np.float32), 25,
        )
    msg = str(exc.value)
    assert "鉴权配置" not in msg, f"不得误导用户检查鉴权: {msg}"
    assert "就绪" in msg, f"应指明节点未就绪: {msg}"


def test_cloud_upload_id_is_deterministic_per_avatar_and_content(fake_asset):
    """upload_id 必须由 (avatar_id, zip 摘要) 确定化：同主播同资产跨次调用一致，换资产即变。"""
    from server.core.avatar.speech_drive_preview import _cloud_upload_id, _sample_cloud_asset_zip

    _zip_a, sha_a = _sample_cloud_asset_zip(fake_asset)
    _zip_b, sha_b = _sample_cloud_asset_zip(fake_asset)

    assert _cloud_upload_id("anchor_a", sha_a) == _cloud_upload_id("anchor_a", sha_a)
    assert _cloud_upload_id("anchor_a", sha_a) == _cloud_upload_id("anchor_a", sha_b)
    assert _cloud_upload_id("anchor_a", sha_a) != _cloud_upload_id("anchor_b", sha_a)
    assert _cloud_upload_id("anchor_a", sha_a) != _cloud_upload_id("anchor_a", "0" * 64)


# ---------------------------------------------------------------------------
# 试播不得复用「实时播放时间线」：那会把收集帧变成按真实时间等待
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_timeline_realtime_pacing_can_be_disabled():
    """关闭实时节奏后，消费端必须立即发布帧，不得按墙钟等待到帧的 PTS 时刻。

    真实故障：试播等待 40 秒后报「sidecar 视频时间线 consumer 已提前终止」。
    根因是 `_play_video_timeline` 为**直播**设计——按 pts_samples 对齐墙钟实时
    推进（直播里音画必须同步）。但试播没有虚拟音频时钟、只是收集帧序列，
    于是被硬生生拖成实时播放：节点渲染 + 隧道传输稍慢于音频时长，
    时间线就会撞上 deadline = 时长 + 5s 而超时退出，后续帧随即报「已提前终止」。
    """
    from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
    from server.core.media.sidecar_protocol import SidecarVideoFrame
    from server.adapters.media.neural_sidecar_driver import _TIMELINE_END

    driver = NeuralSidecarMediaDriver(
        node_url="wss://gpu.example.com/ws/render-v3",
        auth_token="t",
        backend_id="auto",
        avatar_id="default",
    )
    assert driver.realtime_pacing is True, "直播链路默认必须保持实时节奏"
    driver.realtime_pacing = False

    published: list = []

    async def _collect(jpeg, owner=None):
        published.append(jpeg)

    driver._publish_video_frame = _collect  # noqa: SLF001

    jpeg = b"\xff\xd8" + b"x" + b"\xff\xd9"
    queue: asyncio.Queue = asyncio.Queue()
    # pts 远在 10 秒之后：实时模式下会等 10 秒，关闭节奏后必须立刻消费
    frame = SidecarVideoFrame(
        request_id="r", audio_id="a", sequence=0, pts_samples=16000 * 10,
        audio_generation=0, session_generation=0, jpeg=jpeg,
    )
    await queue.put(frame)
    await queue.put(_TIMELINE_END)

    started = time.monotonic()
    await asyncio.wait_for(
        driver._play_video_timeline("a", 16000, 16000 * 10, queue), timeout=5  # noqa: SLF001
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"关闭实时节奏后不应按墙钟等待，实测 {elapsed:.2f}s"
    assert published == [jpeg], f"帧必须被立即发布: {published}"


def test_preview_disables_realtime_pacing_on_driver():
    """试播渲染云端时必须显式关闭实时节奏。"""
    import inspect

    from server.core.avatar import speech_drive_preview as sdp

    src = inspect.getsource(sdp.SpeechDrivePreviewService._render_cloud)
    assert "realtime_pacing" in src, (
        "试播必须关闭实时节奏（realtime_pacing=False），否则收集帧会被拖成实时播放"
    )


# ---------------------------------------------------------------------------
# 云端节点做真实 GPU 推理时会阻塞事件循环，客户端 ping 不能按默认值判死
# ---------------------------------------------------------------------------
def test_sidecar_connect_uses_tolerant_keepalive_settings():
    """连接 sidecar 必须放宽 keepalive（ping_interval / ping_timeout）。

    真实故障：`sent 1011 (internal error) keepalive ping timeout; no close frame
    received`。节点启用真实 GPU 推理后，其 WebSocket 处理器**同步**跑 LatentSync 推理，
    事件循环被阻塞数秒到数十秒，来不及回 pong；而客户端用的是 websockets 默认值
    （ping_interval=20 / ping_timeout=20），于是在节点正忙于推理时把连接判死，
    报一个与真实原因毫无关系的 1011。必须放宽到能容纳单次推理耗时。
    """
    import inspect

    from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver

    src = inspect.getsource(NeuralSidecarMediaDriver._ensure_connection)
    assert "ping_interval" in src, "连接 sidecar 必须显式设置 ping_interval"
    assert "ping_timeout" in src, "连接 sidecar 必须显式设置 ping_timeout"

    from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver as D

    assert D.KEEPALIVE_PING_INTERVAL >= 20.0, "ping 间隔不应短于默认 20s"
    assert D.KEEPALIVE_PING_TIMEOUT >= 120.0, (
        "ping 超时必须远大于默认 20s（真实 GPU 推理会阻塞节点事件循环）"
    )


# ---------------------------------------------------------------------------
# 试播视频体积：隧道只有 ~123 KB/s，传 720x960 全图必然中途断链
# ---------------------------------------------------------------------------
def test_preview_requests_face_only_frames():
    """试播必须向节点请求「仅人脸裁剪帧」，不得拉 720x960 全图。

    实测（对线上节点）：下行 123 KB/s、单帧 50 KB，10 秒音频 = 250 帧 = 12.7MB，
    需要约 101 秒；而连接在 50 秒处被隧道掐断（`no close frame received or sent`）。
    根本原因是带宽带不动，而不是超时不够——加长超时无用，必须减小体积。

    客户端本来就要把画面裁成 256x256 做人脸视图，节点却仍在传 720x960 全图；
    让节点直接只发 256x256 人脸帧，体积可降约 5 倍。
    """
    import inspect

    from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver

    src = inspect.getsource(NeuralSidecarMediaDriver)
    assert '"preview_face_only": self.preview_face_only' in src, (
        "render_open 必须携带 preview_face_only 标记，让节点只发 256x256 人脸帧"
    )


def test_preview_cloud_render_uses_frames_directly_without_recrop():
    """节点已输出 256x256 人脸帧时，客户端不得再按 coords 裁剪。

    否则会对已经裁好的 256 帧再次套用原图坐标裁剪，得到错误的二次裁剪画面。
    """
    import inspect

    from server.core.avatar.speech_drive_preview import SpeechDrivePreviewService

    src = inspect.getsource(SpeechDrivePreviewService._render_cloud)
    assert "_crop_like" in src, (
        "非预览（完整帧）路径仍需保留 coords 裁剪作为兜底"
    )
    assert "preview_face_only" in src or "face_only" in src, (
        "预览模式须显式区分：帧已是 256x256 人脸，不得再裁"
    )

@pytest.mark.anyio
async def test_ensure_cloud_assets_uses_chunked_upload(monkeypatch, fake_asset):
    """弱网场景优先分块上传：块发往 /assets/{id}/chunks，服务端重组后必须与原 zip 逐字节一致"""
    from server.core.avatar.speech_drive_preview import (
        _ensure_cloud_assets,
        _sample_cloud_asset_zip,
        ASSET_UPLOAD_CHUNK_BYTES,
        SidecarConnection,
    )
    import urllib.request

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    server = _FakeSidecar(stored_digest=None)  # 云端无资产 → 触发上传
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    expected_zip = _sample_cloud_asset_zip(fake_asset)[0]
    assert server.stored_zip == expected_zip, "分块重组后必须与本地采样包逐字节一致 (画质无损)"
    assert server.commit_count == 1

    # 所有请求都落在源站根路径 /assets/...，不得误用 WebSocket 路由 /ws/render-v3
    for method, url, _t in server.calls:
        assert "/ws/render-v3" not in url, f"资产请求误用 WebSocket 路由: {url}"
        assert url.startswith("https://fake-sidecar.example.com/assets/test_anchor"), url

    chunk_posts = [c for c in server.calls if c[0] == "POST" and c[1].endswith("/chunks")]
    n = (len(expected_zip) + ASSET_UPLOAD_CHUNK_BYTES - 1) // ASSET_UPLOAD_CHUNK_BYTES
    assert len(chunk_posts) == n, f"分块数应为 {n}，实际 {len(chunk_posts)}"


@pytest.mark.anyio
async def test_ensure_cloud_assets_falls_back_when_chunked_missing(monkeypatch, fake_asset):
    """旧版 sidecar 无分块端点 (404) 时自动回退单次整包上传，资产仍须完整到位"""
    from server.core.avatar.speech_drive_preview import (
        _ensure_cloud_assets,
        _sample_cloud_asset_zip,
        SidecarConnection,
    )
    import urllib.request

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    server = _FakeSidecar(chunked=False, stored_digest=None)
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    assert server.stored_zip == _sample_cloud_asset_zip(fake_asset)[0], "回退整包上传也必须完整"
    assert server.commit_count == 0, "旧端点不应有 commit"
    legacy = [c for c in server.calls if c[0] == "POST" and c[1] == _FakeSidecar.BASE]
    assert len(legacy) == 1, f"应回退到旧版整包端点 1 次，实际 {len(legacy)} 次"


@pytest.mark.anyio
async def test_ensure_cloud_assets_skips_upload_when_digest_matches(monkeypatch, fake_asset):
    """GET 返回的摘要与本地采样包一致时必须跳过上传 (与服务端存储摘要同口径)"""
    from server.core.avatar.speech_drive_preview import (
        _cloud_asset_digest,
        _ensure_cloud_assets,
        _sample_cloud_asset_zip,
        SidecarConnection,
    )
    import urllib.request

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    expected_digest = _cloud_asset_digest(_sample_cloud_asset_zip(fake_asset)[0])
    server = _FakeSidecar(stored_digest=expected_digest)
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    assert [c[0] for c in server.calls] == ["GET"], f"摘要一致时应只发 1 次 GET 探活，实际 {server.calls}"
    assert server.stored_zip is None, "摘要一致时不得上传"


@pytest.mark.anyio
async def test_ensure_cloud_assets_retries_transient_connection_error(monkeypatch, fake_asset):
    """trycloudflare 临时隧道偶发 TLS EOF：GET 幂等探活应对瞬时连接错误重试后成功"""
    import asyncio
    import urllib.error
    import urllib.request

    from server.core.avatar.speech_drive_preview import (
        _cloud_asset_digest,
        _ensure_cloud_assets,
        _sample_cloud_asset_zip,
        SidecarConnection,
    )

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )

    calls: list[str] = []
    expected_digest = _cloud_asset_digest(_sample_cloud_asset_zip(fake_asset)[0])

    class _FakeResp:
        status = 200
        close_calls = 0

        def read(self):
            return f'{{"sha256":"{expected_digest}"}}'.encode()

        def close(self):
            self.close_calls += 1

    def _flaky_urlopen(req, timeout=None):
        calls.append(req.get_full_url())
        if len(calls) < 3:
            raise urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
        return _FakeResp()

    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    monkeypatch.setattr(urllib.request, "urlopen", _flaky_urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    assert len(calls) == 3, f"应重试 2 次后第 3 次成功，实际调用 {len(calls)} 次"
    assert calls[-1] == "https://fake-sidecar.example.com/assets/test_anchor"


@pytest.mark.anyio
async def test_ensure_cloud_assets_retries_post_write_timeout(monkeypatch, fake_asset):
    """分块上传遇到瞬时写超时后应重试该块，最终成功 (慢速隧道场景)"""
    import asyncio
    import urllib.request

    from server.core.avatar.speech_drive_preview import (
        _ensure_cloud_assets,
        _sample_cloud_asset_zip,
        SidecarConnection,
    )

    conn = SidecarConnection(
        node_url="wss://fake-sidecar.example.com/ws/render-v3",
        auth_token="test_token",
        canonical={"avatar_id": "test_anchor"},
        model_name="auto",
    )
    server = _FakeSidecar(stored_digest=None, fail_first_n_posts=1)  # 首个块写超时
    _orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _orig_sleep(0))
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)

    await _ensure_cloud_assets(conn, fake_asset)

    assert server.stored_zip == _sample_cloud_asset_zip(fake_asset)[0], "重试后资产仍须完整"
    # 大包写超时必须按载荷放宽 (远大于旧的 60s 硬超时)
    posts = [c for c in server.calls if c[0] == "POST"]
    assert all(t > 60 for _, _, t in posts), f"POST 超时应按载荷放宽，实际 {posts[0][2]}"


@pytest.mark.anyio
async def test_speech_drive_preview_latentsync_hardware_gate(fake_asset):
    """latentsync 模式下若云端 GPU 不达标，应被硬性门禁拦截"""
    import numpy as np
    from server.core.avatar.speech_drive_preview import (
        PreviewHardwareError,
        SpeechDrivePreviewService,
    )
    from server.core.hardware.gpu_capability import ComputePlan

    svc = SpeechDrivePreviewService()
    # 模拟无合格云端 GPU 的计算方案
    fake_plan = ComputePlan(
        use_cloud=False,
        has_cloud_gpu=False,
        local_gpu={"cuda_available": False, "vram_total_gb": 0.0},
        cloud_gpu=None,
    )
    import unittest.mock
    svc._resolve_compute_plan = unittest.mock.AsyncMock(return_value=fake_plan)

    sr = 16000
    pcm = (np.sin(np.linspace(0, 440 * 2 * np.pi, sr // 2)) * 30000).astype(np.int16).tobytes()

    with pytest.raises(PreviewHardwareError, match="硬件不足"):
        await svc.run(
            anchor_id="anchor_1",
            asset_dir=fake_asset,
            audio_bytes=pcm,
            render_mode="latentsync",
        )


@pytest.mark.anyio
async def test_speech_drive_preview_latentsync_success(monkeypatch, fake_asset):
    """latentsync 模式在云端批处理端点返回有效帧时，能正确解码、融合并产出高精结果"""
    import base64
    import cv2
    import numpy as np
    from server.core.avatar.speech_drive_preview import (
        RenderOutcome,
        SpeechDrivePreviewService,
    )
    from server.core.hardware.gpu_capability import ComputePlan

    svc = SpeechDrivePreviewService()
    fake_plan = ComputePlan(
        use_cloud=True,
        has_cloud_gpu=True,
        local_gpu={"cuda_available": False, "vram_total_gb": 0.0},
        cloud_gpu={"is_reachable": True, "vram_total_gb": 16.0, "gpu_name": "NVIDIA A100"},
    )
    import unittest.mock
    svc._resolve_compute_plan = unittest.mock.AsyncMock(return_value=fake_plan)

    # 构造一张测试 256x256 人脸图片并转为 base64
    face = np.full((256, 256, 3), 128, dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", face)
    b64_frame = base64.b64encode(buf.tobytes()).decode("ascii")

    # 模拟 _render_cloud_batch_latentsync 返回合成结果
    outcome = RenderOutcome(
        face_frames=[face, face],
        full_frames=[face, face],
        timings_ms=[120.0, 115.0],
        device="NVIDIA A100 (LatentSync/Diffusion)",
        providers=["cloud:latentsync_unet3d"],
        vram_total_gb=16.0,
    )
    svc._render_cloud_batch_latentsync = unittest.mock.AsyncMock(return_value=outcome)

    # 构造有效 16k 单声道 PCM 音频
    sr = 16000
    pcm = (np.sin(np.linspace(0, 440 * 2 * np.pi, sr // 2)) * 30000).astype(np.int16).tobytes()

    result = await svc.run(
        anchor_id="anchor_1",
        asset_dir=fake_asset,
        audio_bytes=pcm,
        render_mode="latentsync",
    )

    assert result.engine == "neural_cloud_latentsync"
    assert result.mode == "latentsync_batch"
    assert result.frame_count == 2
    assert "latentsync" in result.device.lower()


# ---------------------------------------------------------------------------
# 分段渲染 (真实故障修复)：云端 45s 安全窗口只返回部分帧时，必须按返回帧数
# 切分音频继续请求直至全部帧完成神经渲染；仍有缺口时如实告知，绝不静默补帧
# ---------------------------------------------------------------------------
def _textured_face_b64(value_seed: int = 0) -> str:
    """构造带真实纹理 (corner_std > 5) 的 256x256 测试帧并编码为 JPEG base64。

    必须带纹理：纯色帧会触发「灰底融合保护」，被本地底片 (全零) 羽化覆盖，
    导致测试无法区分「神经渲染帧」与「底片补帧」。
    """
    import base64

    face = np.zeros((256, 256, 3), dtype=np.uint8)
    face[:, :, 0] = (np.arange(256, dtype=np.uint8) + value_seed)[:, None]
    face[:, :, 1] = np.arange(256, dtype=np.uint8)[None, :]
    _, buf = cv2.imencode(".jpg", face)
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _latentsync_service(monkeypatch, fake_asset, *, captured_conns=None):
    """构造可直接调用 _render_cloud_batch_latentsync 的服务实例 (mock 连接/资产同步)"""
    from server.core.avatar.speech_drive_preview import (
        SpeechDrivePreviewService,
        SidecarConnection,
    )

    svc = SpeechDrivePreviewService()

    async def _fake_resolve(cloud_gpu):
        return SidecarConnection(
            node_url="wss://gpu.example.com/ws/render-v3",
            auth_token="token",
            canonical={"backend_id": "latentsync", "avatar_id": "default"},
            model_name="auto",
        )

    async def _fake_ensure(conn, asset_dir):
        if captured_conns is not None:
            captured_conns.append(conn)

    monkeypatch.setattr(svc, "_resolve_sidecar_connection", _fake_resolve)
    monkeypatch.setattr(
        "server.core.avatar.speech_drive_preview._ensure_cloud_assets", _fake_ensure
    )
    return svc


@pytest.mark.anyio
async def test_latentsync_segmented_render_completes_truncated_frames(monkeypatch, fake_asset):
    """云端单轮只完成部分帧 (45s 安全窗口截断) 时，必须从未完成帧号的音频位置继续请求，
    直至全部帧完成真实神经渲染——绝不允许第二句开始闭口。"""
    from server.core.avatar.speech_drive_preview import SAMPLES_PER_FRAME_16K

    svc = _latentsync_service(monkeypatch, fake_asset)
    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000 * 2)) * 0.2).astype(np.float32)  # 2s = 50 帧
    n_frames = 50
    frame_b64 = _textured_face_b64()
    calls: list[np.ndarray] = []
    face_b64_log: list[list] = []

    async def _fake_post(batch_url, headers, seg_pcm, face_imgs_b64, anchor_id):
        calls.append(seg_pcm.copy())
        face_b64_log.append(list(face_imgs_b64))
        # 第 1 轮：45s 窗口内只完成 30 帧 (对应真实故障 ~80/201 帧)；第 2 轮：补齐剩余 20 帧
        return [frame_b64] * (30 if len(calls) == 1 else 20)

    monkeypatch.setattr(svc, "_post_latentsync_batch", _fake_post)

    outcome = await svc._render_cloud_batch_latentsync(
        {"gpu_name": "NVIDIA A100", "vram_total_gb": 80.0}, fake_asset, pcm, "anchor_seg", n_frames
    )

    assert len(calls) == 2, "截断后必须再次请求补全，而不是拿部分帧凑数"
    assert len(calls[0]) == 16000 * 2, "第 1 轮应携带整段音频"
    assert len(calls[1]) == len(pcm) - 30 * SAMPLES_PER_FRAME_16K, (
        f"第 2 轮必须从未完成帧号 (30 帧 × 640 采样) 对应的音频位置继续，"
        f"实际长度 {len(calls[1])}"
    )
    # 切片兜底契约：每个分段都必须随行携带本地切片。
    # 云端节点历史缺陷 load_face_imgs() 漏 return → 资产目录切片被加载后丢弃 →
    # 无切片时落到灰底占位图 (210,220,240) 造成整段白屏；后续分段省略兜底即会复发。
    assert len(face_b64_log[0]) > 0, "首轮应携带本地切片兜底"
    assert len(face_b64_log[1]) > 0, "后续分段也必须携带切片兜底，否则节点降级为灰底白屏"
    assert len(outcome.face_frames) == n_frames
    assert len(outcome.full_frames) == n_frames
    assert outcome.fallback_reason is None, "全部帧已完成神经渲染，不得声称有缺口"
    # 全部 50 帧都来自云端渲染 (带纹理)，绝无底片补帧 (fake_asset 的 face 全零)
    assert all(f.mean() > 50 for f in outcome.face_frames), "存在被底片覆盖的帧：分段补全未生效"
    assert len(outcome.timings_ms) == n_frames


@pytest.mark.anyio
async def test_latentsync_segmented_render_reports_honest_fallback_on_stall(monkeypatch, fake_asset):
    """节点后续轮次停滞 (空返回) 时：补帧保持时长连续，但必须如实标注缺口，绝不静默。"""
    svc = _latentsync_service(monkeypatch, fake_asset)
    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000 * 2)) * 0.2).astype(np.float32)  # 2s = 50 帧
    n_frames = 50
    frame_b64 = _textured_face_b64()
    calls = 0

    async def _stalled_post(batch_url, headers, seg_pcm, face_imgs_b64, anchor_id):
        nonlocal calls
        calls += 1
        # 第 1 轮完成 30 帧，此后节点停滞 (空返回)
        return [frame_b64] * 30 if calls == 1 else []

    monkeypatch.setattr(svc, "_post_latentsync_batch", _stalled_post)

    outcome = await svc._render_cloud_batch_latentsync(
        {"gpu_name": "NVIDIA A100", "vram_total_gb": 80.0}, fake_asset, pcm, "anchor_stall", n_frames
    )

    assert calls == 2, "停滞后应保留已完成帧并退出，不得无限重试"
    assert len(outcome.face_frames) == n_frames and len(outcome.full_frames) == n_frames
    # 前 30 帧为神经渲染 (带纹理)，后 20 帧为主播底片 (全零)
    assert all(f.mean() > 50 for f in outcome.face_frames[:30])
    assert all(f.mean() == 0 for f in outcome.face_frames[30:]), "缺口帧必须衔接底片保持时长连续"
    assert outcome.fallback_reason and "底片" in outcome.fallback_reason and "30/50" in outcome.fallback_reason, (
        f"必须如实告知仅完成 30/50 帧，实际: {outcome.fallback_reason}"
    )
    assert len(outcome.timings_ms) == n_frames


def _gray_placeholder_b64() -> str:
    """云端节点缺失切片时的灰底占位帧 (210,220,240)：灰度均值 224.8、std < 1.5。"""
    import base64

    face = np.full((256, 256, 3), (210, 220, 240), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", face)
    return base64.b64encode(buf.tobytes()).decode("ascii")


def test_degraded_frame_detection_flags_gray_placeholder():
    """灰底占位图必须被判为降级坏帧，真实人脸纹理帧不得误判。"""
    from server.core.avatar.speech_drive_preview import _is_degraded_frame

    placeholder = np.full((256, 256, 3), (210, 220, 240), dtype=np.uint8)
    assert _is_degraded_frame(placeholder) is True

    textured = np.zeros((256, 256, 3), dtype=np.uint8)
    textured[:, :, 0] = np.arange(256, dtype=np.uint8)[:, None]
    textured[:, :, 1] = np.arange(256, dtype=np.uint8)[None, :]
    assert _is_degraded_frame(textured) is False


@pytest.mark.anyio
async def test_latentsync_replaces_gray_placeholder_frames(monkeypatch, fake_asset):
    """云端输出灰底白屏帧时，客户端必须回退主播底片 (绝不白屏)，并如实标注缺口。

    真实故障：云端节点 load_face_imgs() 漏 return，切片被加载后丢弃 →
    无切片可用 → 直接返回灰底占位图 (210,220,240) → 末尾整段白屏。
    """
    svc = _latentsync_service(monkeypatch, fake_asset)
    pcm = (np.sin(np.linspace(0, 100 * np.pi, 16000)) * 0.2).astype(np.float32)  # 1s = 25 帧
    gray_b64 = _gray_placeholder_b64()
    good_b64 = _textured_face_b64()

    async def _fake_post(batch_url, headers, seg_pcm, face_imgs_b64, anchor_id):
        # 第 1 轮：前 10 帧正常；第 2 轮：整段灰底白屏
        if not getattr(svc, "_round1_done", False):
            svc._round1_done = True
            return [good_b64] * 10
        return [gray_b64] * 15

    monkeypatch.setattr(svc, "_post_latentsync_batch", _fake_post)

    outcome = await svc._render_cloud_batch_latentsync(
        {"gpu_name": "Ascend 910B2", "vram_total_gb": 64.0}, fake_asset, pcm, "anchor_gray", 25
    )

    assert len(outcome.face_frames) == 25
    # 前 10 帧保留云端渲染 (带纹理)；后 15 帧必须回退主播底片 (fake_asset 的 face 全零)
    assert all(f.mean() > 50 for f in outcome.face_frames[:10])
    assert all(f.mean() == 0 for f in outcome.face_frames[10:]), "灰底白屏帧必须回退底片，绝不能白屏"
    assert outcome.fallback_reason and "15/25" in outcome.fallback_reason, (
        f"必须如实标注降级帧数，实际: {outcome.fallback_reason}"
    )


@pytest.mark.anyio
async def test_latentsync_uses_per_anchor_asset_key(monkeypatch, fake_asset):
    """云端资产必须按主播隔离：canonical.avatar_id 恒取 anchor_id，杜绝所有主播
    共用 "default" 键互相覆盖资产、且每次切换主播都触发整包重传。"""
    from server.core.avatar.speech_drive_preview import SidecarConnection

    captured: list[SidecarConnection] = []
    svc = _latentsync_service(monkeypatch, fake_asset, captured_conns=captured)
    pcm = np.zeros(16000, dtype=np.float32)  # 1s

    async def _fake_post(batch_url, headers, seg_pcm, face_imgs_b64, anchor_id):
        return [_textured_face_b64()]

    monkeypatch.setattr(svc, "_post_latentsync_batch", _fake_post)

    await svc._render_cloud_batch_latentsync(
        {"gpu_name": "NVIDIA A100", "vram_total_gb": 80.0}, fake_asset, pcm, "anchor_iso", 25
    )

    assert captured and captured[0].canonical.get("avatar_id") == "anchor_iso", (
        f"资产同步必须使用 anchor_id 作为云端资产键，实际: {captured[0].canonical.get('avatar_id')}"
    )


@pytest.mark.anyio
async def test_run_propagates_latentsync_partial_render_fallback(monkeypatch, fake_asset, tmp_path):
    """run() 必须把分段渲染缺口如实透传到 SpeechDriveResult.fallback_reason，
    供 API 响应与前端状态条展示——历史缺陷是恒置 None，静默吞掉缺口。"""
    import unittest.mock

    from server.core.avatar.speech_drive_preview import (
        PREVIEW_SESSION_ROOT,
        RenderOutcome,
        SpeechDrivePreviewService,
    )
    from server.core.hardware.gpu_capability import ComputePlan

    svc = SpeechDrivePreviewService()
    svc._sessions_root = tmp_path / "sessions"
    fake_plan = ComputePlan(
        use_cloud=True,
        has_cloud_gpu=True,
        local_gpu={"cuda_available": False, "vram_total_gb": 0.0},
        cloud_gpu={"is_reachable": True, "vram_total_gb": 16.0, "gpu_name": "NVIDIA A100"},
    )
    svc._resolve_compute_plan = unittest.mock.AsyncMock(return_value=fake_plan)

    face = np.full((256, 256, 3), 128, dtype=np.uint8)
    outcome = RenderOutcome(
        face_frames=[face, face],
        full_frames=[face, face],
        timings_ms=[120.0, 115.0],
        device="NVIDIA A100 (LatentSync/Diffusion)",
        providers=["cloud:latentsync_unet3d"],
        vram_total_gb=16.0,
        fallback_reason="云端节点在代理安全窗口内仅完成前 2/12 帧神经渲染，其余帧为主播底片（无口型）",
    )
    svc._render_cloud_batch_latentsync = unittest.mock.AsyncMock(return_value=outcome)

    sr = 16000
    audio = (np.sin(np.linspace(0, 440 * 2 * np.pi, sr // 2)) * 30000).astype(np.int16).tobytes()
    try:
        result = await svc.run(
            anchor_id="anchor_partial",
            asset_dir=fake_asset,
            audio_bytes=audio,
            render_mode="latentsync",
        )
    finally:
        svc._sessions_root = PREVIEW_SESSION_ROOT

    assert result.engine == "neural_cloud_latentsync"
    assert result.fallback_reason == outcome.fallback_reason, "分段渲染缺口必须如实透传，不得静默吞掉"


# ---------------------------------------------------------------------------
# 试播结果缓存复用：同「主播+台词+音色+资产版本」重复试听秒开，
# 跳过 TTS 与数分钟云端逐帧渲染；残缺会话 (含底片补帧) 绝不命中
# ---------------------------------------------------------------------------
def _run_with_mocked_latentsync(svc, outcome, *, anchor_id, cache_key, fake_asset):
    """以 mocked 算力方案与渲染结果驱动一次 service.run (落盘真实会话目录)"""
    import unittest.mock

    from server.core.hardware.gpu_capability import ComputePlan

    fake_plan = ComputePlan(
        use_cloud=True,
        has_cloud_gpu=True,
        local_gpu={"cuda_available": False, "vram_total_gb": 0.0},
        cloud_gpu={"is_reachable": True, "vram_total_gb": 16.0, "gpu_name": "NVIDIA A100"},
    )
    svc._resolve_compute_plan = unittest.mock.AsyncMock(return_value=fake_plan)
    svc._render_cloud_batch_latentsync = unittest.mock.AsyncMock(return_value=outcome)
    audio = (np.sin(np.linspace(0, 440 * 2 * np.pi, 16000 // 2)) * 30000).astype(np.int16).tobytes()
    return svc.run(
        anchor_id=anchor_id,
        asset_dir=fake_asset,
        audio_bytes=audio,
        render_mode="latentsync",
        cache_key=cache_key,
    )


@pytest.mark.anyio
async def test_run_persists_cache_meta_and_reuse_hits(monkeypatch, fake_asset, tmp_path):
    """同 cache_key 的重复试听必须直接复用历史会话 (秒开)，不同 key 不得命中。"""
    from server.core.avatar.speech_drive_preview import (
        PREVIEW_SESSION_ROOT,
        RenderOutcome,
        SpeechDrivePreviewService,
    )

    svc = SpeechDrivePreviewService()
    svc._sessions_root = tmp_path / "sessions"
    face = np.full((256, 256, 3), 128, dtype=np.uint8)
    outcome = RenderOutcome(
        face_frames=[face, face],
        full_frames=[face, face],
        timings_ms=[120.0, 115.0],
        device="NVIDIA A100 (LatentSync/Diffusion)",
        providers=["cloud:latentsync_unet3d"],
        vram_total_gb=16.0,
    )
    try:
        result = await _run_with_mocked_latentsync(
            svc, outcome, anchor_id="anchor_cache", cache_key="key_a", fake_asset=fake_asset
        )
        assert result.reused is False

        cached = svc.get_cached_result("anchor_cache", "key_a")
        assert cached is not None, "同台词重复试听必须命中缓存"
        assert cached.reused is True
        assert cached.session_id == result.session_id
        assert cached.frame_count == 2
        assert cached.engine == "neural_cloud_latentsync"
        assert cached.fallback_reason is None

        assert svc.get_cached_result("anchor_cache", "key_b") is None, "不同台词/音色不得命中"
        assert svc.get_cached_result("anchor_other", "key_a") is None, "不同主播不得命中"
    finally:
        svc._sessions_root = PREVIEW_SESSION_ROOT


@pytest.mark.anyio
async def test_partial_render_session_is_never_reused(monkeypatch, fake_asset, tmp_path):
    """含底片补帧的残缺会话必须重渲，绝不能把「第二句闭口」的历史结果当缓存秒开。"""
    from server.core.avatar.speech_drive_preview import (
        PREVIEW_SESSION_ROOT,
        RenderOutcome,
        SpeechDrivePreviewService,
    )

    svc = SpeechDrivePreviewService()
    svc._sessions_root = tmp_path / "sessions"
    face = np.full((256, 256, 3), 128, dtype=np.uint8)
    outcome = RenderOutcome(
        face_frames=[face, face],
        full_frames=[face, face],
        timings_ms=[120.0, 115.0],
        device="NVIDIA A100 (LatentSync/Diffusion)",
        providers=["cloud:latentsync_unet3d"],
        vram_total_gb=16.0,
        fallback_reason="云端节点在代理安全窗口内仅完成前 2/12 帧神经渲染，其余帧为主播底片（无口型）",
    )
    try:
        await _run_with_mocked_latentsync(
            svc, outcome, anchor_id="anchor_partial_cache", cache_key="key_p", fake_asset=fake_asset
        )
        assert svc.get_cached_result("anchor_partial_cache", "key_p") is None, (
            "残缺会话 (含底片补帧) 不得命中缓存，必须重新完整渲染"
        )
    finally:
        svc._sessions_root = PREVIEW_SESSION_ROOT


