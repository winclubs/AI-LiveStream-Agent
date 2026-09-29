# -*- coding: utf-8 -*-
"""试播台词驱动编排服务测试"""
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
            def read(self):
                return self._body

        class _Err(urllib.error.HTTPError):
            def __init__(self, code, body=b""):
                self.code, self._body = code, body
            def read(self):
                return self._body

        if method == "GET":
            if self.stored_digest is None:
                raise _Err(404)
            return _Resp(200, json.dumps({"sha256": self.stored_digest}).encode())

        if self.fail_first_n_posts > 0:
            self.fail_first_n_posts -= 1
            raise urllib.error.URLError("The write operation timed out")

        body = json.loads(req.data.decode("utf-8"))
        if url.endswith("/chunks"):
            if not self.chunked:
                raise _Err(404)
            self.chunks.setdefault(body["upload_id"], {})[body["index"]] = base64.b64decode(body["data"])
            return _Resp(200, b'{"index": 0, "received": 1}')
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
            return _Resp(200, b'{"face_count": 1}')
        if url == self.BASE:  # 旧版整包上传
            self.stored_zip = base64.b64decode(body["zip_base64"])
            return _Resp(200, b'{"face_count": 1}')
        raise _Err(404)


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

        def read(self):
            return f'{{"sha256":"{expected_digest}"}}'.encode()

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

