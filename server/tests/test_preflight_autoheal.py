# -*- coding: utf-8 -*-
"""体检自愈 (预检自动触发下载/安装) 的集成测试"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server.core.audio.asr_engine as asr_engine
from server.app import app
from server.core.audio.asr_dependency_installer import AsrDependencyInstaller
from server.core.avatar.lipsync_weight_downloader import LipSyncWeightDownloader


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _check_by_key(checks, key):
    return next((c for c in checks if c["key"] == key), None)


def test_lipsync_downloader_retry_cooldown():
    """神经唇形权重下载失败后冷却期内不重复触发"""
    dl = LipSyncWeightDownloader()
    with patch.object(LipSyncWeightDownloader, "is_weight_ready", return_value=False):
        import time as _t

        dl._state = "failed"
        dl._finished_at = _t.time()
        assert dl.can_auto_retry() is False
        result = dl.start_download()
        assert result["ok"] is False
        assert result["reason"] == "retry_cooldown"
    dl._state = "idle"


def test_lipsync_downloader_force_bypasses_cooldown():
    dl = LipSyncWeightDownloader()
    with patch.object(LipSyncWeightDownloader, "is_weight_ready", return_value=False):
        import time as _t

        dl._state = "failed"
        dl._finished_at = _t.time()
        result = dl.start_download(force=True)
        assert result["ok"] is True
        assert result["reason"] == "started"
    dl._state = "idle"


def test_preflight_reports_installed_asr_as_pass(client):
    """已安装 faster-whisper 但引擎尚未懒加载时，体检应如实判定为通过而非缺陷"""
    asr_engine._asr_model = None
    asr_engine._asr_backend_type = None
    try:
        res = client.get("/api/v1/live/preflight")
        assert res.status_code == 200
        asr_check = _check_by_key(res.json()["data"]["checks"], "asr_engine")
        assert asr_check is not None
        if asr_engine.is_faster_whisper_installed():
            # 依赖已装即判定通过，消息明确告知"首次语音输入自动懒加载"
            assert asr_check["status"] == "pass"
            assert "自动懒加载" in asr_check["message"]
    finally:
        asr_engine._asr_model = None
        asr_engine._asr_backend_type = None


def test_preflight_lipsync_check_no_manual_download_hint(client):
    """体检 lipsync_weight 项不得再出现要求用户手动调用接口或执行脚本的指引"""
    res = client.get("/api/v1/live/preflight")
    assert res.status_code == 200
    lip_check = _check_by_key(res.json()["data"]["checks"], "lipsync_weight")
    assert lip_check is not None
    # 无论就绪/下载中/缺失，指引都不应把下载动作甩给用户
    assert "POST /api/v1" not in (lip_check.get("fix_hint") or "")
    assert "download_weights.py" not in (lip_check.get("fix_hint") or "")


def test_preflight_auto_in_progress_flag_shape(client):
    """auto_in_progress 标记必须存在且为布尔，供前端轮询进度"""
    res = client.get("/api/v1/live/preflight")
    assert res.status_code == 200
    for c in res.json()["data"]["checks"]:
        assert isinstance(c.get("auto_in_progress"), bool)


def test_preflight_never_claims_ready_while_neural_lipsync_unavailable(client):
    """ADR-16：神经唇形不可用时，体检**任何一项**都不得宣称"就绪开播"。

    历史缺陷：`lipsync_weight` 项已如实改为"无法开播"，但 `avatar_engine` 项
    仍宣称"内置高保真微动态引擎 (RealAvatarLite) 就绪开播 / 零显存稳定开播"。
    而真实开播路径 `ProceduralAvatarDriver.start()` 在 ONNX 权重或主播切片
    缺失时**直接 raise 拒绝启动**，程序化渲染器也已按硬性规约移除出生产路径
    （其帧从不上屏）。两套话术并存会让用户以为能开播、点下去却失败。

    RealAvatarLite 已被移出渲染链路，因此其名字不应再出现在体检结论里。
    """
    from server.core.avatar.neural_model_manager import global_neural_model_manager

    with patch.object(
        global_neural_model_manager.__class__,
        "get_model_status_matrix",
        return_value={"has_neural_model": False},
    ):
        res = client.get("/api/v1/live/preflight")

    assert res.status_code == 200
    checks = res.json()["data"]["checks"]
    engine_check = _check_by_key(checks, "avatar_engine")
    assert engine_check is not None, "体检缺少 avatar_engine 项"

    blob = " ".join(
        str(engine_check.get(k) or "") for k in ("status", "title", "message", "fix_hint")
    )
    # 已移出渲染链路的程序化引擎不得再作为"开播就绪"的依据出现
    assert "RealAvatarLite" not in blob, f"体检仍以已下线的 RealAvatarLite 宣称就绪: {blob}"
    # 也不得出现这类"不影响开播"的暗示
    for banned in ("就绪开播", "稳定开播", "直播不受影响"):
        assert banned not in blob, f"体检含误导开播预期的话术 '{banned}': {blob}"

    # 神经模型缺失时，这一项不允许是 pass
    assert engine_check["status"] != "pass", (
        f"无神经模型时 avatar_engine 不得判定 pass: {engine_check}"
    )


def test_asr_install_status_endpoint(client):
    res = client.get("/api/v1/live/asr/install-status")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["package"] == "faster-whisper==1.1.1"
    assert "state" in data
    assert "is_busy" in data


def test_asr_install_dependency_endpoint_is_idempotent(client):
    """调用安装接口对已安装环境立即返回 already_installed，不重复安装"""
    with patch.object(AsrDependencyInstaller, "is_installed", return_value=True):
        res = client.post("/api/v1/live/asr/install-dependency")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    assert body["reason"] == "already_installed"
