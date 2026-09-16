import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_cover_crop_face_box_transform():
    from server.core.media.procedural_renderer import build_cover_transform, transform_face_box

    transform = build_cover_transform(1600, 900, 720, 960)
    assert transform == {
        "source_size": [1600, 900],
        "output_size": [720, 960],
        "resized_size": [1706, 960],
        "crop_offset": [493, 0],
    }
    assert transform_face_box([600, 180, 400, 400], transform) == (146, 192, 428, 427)

    identity = build_cover_transform(720, 960, 720, 960)
    assert transform_face_box([11, 22, 130, 160], identity) == (11, 22, 130, 160)

    clipped = transform_face_box([0, 0, 100, 100], transform)
    assert clipped is None, "被中心裁切完全移出画面的框应回退比例定位"


def test_media_router_reports_effective_capability():
    from server.adapters.media.media_router import MediaDriverRouter

    router = MediaDriverRouter()
    status = router.get_preview_status()
    if status["cv_available"]:
        assert status["driver_type"] == "procedural_avatar"
        assert status["render_backend"] == "procedural"
        assert status["capabilities"]["neural_lipsync"] is False
    assert status["external_publish"]["status"] == "not_managed"
    assert status["external_publish"]["validation"] == "pending_external_acceptance"


def test_live_status_distinguishes_local_source_from_external_publish():
    from server.routes.live import get_live_status, global_live_controller

    old_live = global_live_controller.is_live
    old_sid = global_live_controller.session_id
    try:
        global_live_controller.is_live = True
        global_live_controller.session_id = "sess_boundary"
        payload = asyncio.run(get_live_status())
        assert payload["is_live"] is True
        assert payload["local_source"]["status"] == "running"
        assert payload["external_publish"]["status"] == "not_managed"
        assert payload["external_publish"]["platform_live"] is None
    finally:
        global_live_controller.is_live = old_live
        global_live_controller.session_id = old_sid


def test_console_uses_local_source_wording():
    root = Path(__file__).parents[2]
    js_modules = (root / "server/static/js/modules").glob("*.js")
    js = "".join(f.read_text(encoding="utf-8") for f in sorted(js_modules)) + (root / "server/static/js/console.js").read_text(encoding="utf-8")
    html = (root / "server/static/index.html").read_text(encoding="utf-8")
    assert "正在直播推流中" not in js
    assert "一键开播推流" not in js
    assert "一键开播推流" not in html
    assert "本地直播源运行中" in js
    assert "启动本地直播源" in html


def test_voice_local_features_are_not_reported_as_clone(client):
    wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    upload = client.post(
        "/api/v1/voices/clone",
        data={"name": "能力边界音色", "speed": 1.0, "volume": 1.0},
        files={"audio_file": ("boundary.wav", wav, "audio/wav")},
    )
    assert upload.status_code == 200
    voice_id = upload.json()["data"]["id"]
    assert upload.json()["data"]["synthesis_status"] == "not_available"

    result = client.post(f"/api/v1/voices/{voice_id}/clone")
    assert result.status_code == 200
    data = result.json()["data"]
    if data["engine"] == "local_features":
        assert data["synthesis_status"] == "not_available"
        assert "克隆完成" not in result.json()["message"]
    else:
        assert data["engine"] == "cosyvoice"
        assert data["synthesis_status"] == "reference_ready"
    assert client.delete(f"/api/v1/voices/{voice_id}").status_code == 200


def test_avatar_provider_supports_custom_official_url():
    from server.adapters.media.avatar_provider_registry import (
        list_avatar_provider_descriptors,
        normalize_avatar_provider_config,
    )

    descriptors = list_avatar_provider_descriptors()
    cloud_provider_ids = {"sidecar_v3", "liveavatar_lite", "aliyun_avatar", "tencent_avatar", "custom_avatar"}
    for d in descriptors:
        if d.adapter_id in cloud_provider_ids:
            paths = [f["path"] for f in d.ui_fields]
            assert "extra_params.custom_official_url" in paths, f"{d.adapter_id} ui_fields 应包含 custom_official_url"

    assert any(d.adapter_id == "custom_avatar" for d in descriptors), "必须包含选项 6 custom_avatar"

    custom_url = "https://my-custom-avatar.internal.example.com/console"
    sidecar_canonical = normalize_avatar_provider_config(
        {
            "adapter": "sidecar_v3",
            "custom_official_url": custom_url,
        },
        base_url="ws://127.0.0.1:8010/ws/render-v3",
        credential_present=False,
    )
    assert sidecar_canonical["custom_official_url"] == custom_url

    custom_canonical = normalize_avatar_provider_config(
        {
            "adapter": "custom_avatar",
            "stream_protocol": "rtmp",
            "avatar_id": "my_anchor_1",
            "custom_official_url": custom_url,
        },
        base_url="rtmp://127.0.0.1:1935/live/avatar",
        credential_present=False,
    )
    assert custom_canonical["adapter"] == "custom_avatar"
    assert custom_canonical["stream_protocol"] == "rtmp"
    assert custom_canonical["custom_official_url"] == custom_url

    tencent_canonical = normalize_avatar_provider_config(
        {
            "adapter": "tencent_avatar",
            "virtualman_project_id": "proj_test_123",
            "custom_official_url": custom_url,
        },
        base_url="https://gw.tvs.qq.com",
        credential_present=True,
    )
    assert tencent_canonical["custom_official_url"] == custom_url
