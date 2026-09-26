# -*- coding: utf-8 -*-
"""试播台词驱动编排服务测试"""
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

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
async def test_service_falls_back_honestly(monkeypatch, fake_asset, no_gpu):
    """无 GPU 且无云端时必须诚实降级，且不得伪造设备信息"""
    from server.core.avatar.speech_drive_preview import (
        ENGINE_FALLBACK,
        PREVIEW_SESSION_ROOT,
        get_speech_drive_preview_service,
    )

    service = get_speech_drive_preview_service()
    service._sessions_root = fake_asset / "sessions"
    # 降级渲染用真实素材路径
    audio = _silence_mp3()
    try:
        result = await service.run("anchor_test", fake_asset, audio)
    finally:
        service._sessions_root = PREVIEW_SESSION_ROOT
    assert result.engine == ENGINE_FALLBACK
    assert result.mode == "fallback"
    assert result.device == ""
    assert result.providers == []
    assert result.fallback_reason is not None
    assert result.frame_count > 0


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


def test_endpoint_smoke_success_path(client, anchor_with_asset, fake_asset, monkeypatch):
    """成功路径：mock TTS 合成返回静音 WAV，本地无模型时诚实降级"""
    import server.core.audio.tts_preview_service as tps

    async def _fake_synth(params):
        return _silence_mp3(), "audio/wav"

    monkeypatch.setattr(tps, "synthesize_preview_audio", _fake_synth)

    res = client.post(
        f"/api/v1/anchors/{anchor_with_asset}/avatar/preview-speech-drive",
        json={"text": "大家好，欢迎来到直播间"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["frame_count"] > 0
    assert len(data["face_frames"]) == data["frame_count"]
    assert len(data["full_frames"]) == data["frame_count"]

    # 音频地址可访问
    audio_res = client.get(data["audio_url"])
    assert audio_res.status_code == 200

    # 第一帧 face/full 可访问
    face_res = client.get(data["face_frames"][0])
    assert face_res.status_code == 200
    full_res = client.get(data["full_frames"][0])
    assert full_res.status_code == 200


def test_preview_session_frame_rejects_path_traversal(client, anchor_with_asset):
    """静态帧路由对 ../ 与非法 session_id 返回 404"""
    base = f"/api/v1/anchors/{anchor_with_asset}/preview-sessions"
    assert client.get(f"{base}/00000000000000000000/face/0.jpg").status_code == 404
    assert client.get(f"{base}/..%2F..%2Fface/0.jpg").status_code in (404, 422)
    assert client.get(f"{base}/00000000000000000000/audio.mp3").status_code == 404
