# -*- coding: utf-8 -*-
"""ASR 引擎后端选择与状态上报的单元测试 (修复三)"""
import server.core.audio.asr_engine as asr_engine
from server.core.audio.asr_engine import get_asr_status


def test_get_asr_status_reports_backend_and_readiness():
    status = get_asr_status()
    assert "backend" in status
    assert "ready" in status
    assert "model_name" in status
    assert "message" in status


def test_get_asr_status_when_uninitialized():
    # 尚未懒加载时 backend 为 None，ready 为 False，且不触发真实加载
    asr_engine._asr_model = None
    asr_engine._asr_backend_type = None
    status = get_asr_status()
    assert status["backend"] is None
    assert status["ready"] is False
    assert "尚未初始化" in status["message"]


def test_get_asr_status_faster_whisper_ready():
    asr_engine._asr_backend_type = "faster_whisper"
    asr_engine._asr_model = object()  # 非 None 占位
    try:
        status = get_asr_status()
        assert status["backend"] == "faster_whisper"
        assert status["ready"] is True
        assert status["model_name"] == asr_engine._FASTER_WHISPER_MODEL_NAME
        assert "轻量" in status["message"]
    finally:
        asr_engine._asr_model = None
        asr_engine._asr_backend_type = None


def test_get_asr_status_sensevoice_ready():
    asr_engine._asr_backend_type = "sensevoice"
    asr_engine._asr_model = object()
    try:
        status = get_asr_status()
        assert status["backend"] == "sensevoice"
        assert status["ready"] is True
        assert status["model_name"] == "SenseVoiceSmall"
        assert "高精度" in status["message"]
    finally:
        asr_engine._asr_model = None
        asr_engine._asr_backend_type = None


def test_get_asr_status_mock_fallback():
    asr_engine._asr_backend_type = "mock_fallback"
    asr_engine._asr_model = None
    try:
        status = get_asr_status()
        assert status["backend"] == "mock_fallback"
        assert status["ready"] is False
        assert "未安装" in status["message"]
    finally:
        asr_engine._asr_backend_type = None


def test_faster_whisper_cache_dir_exists():
    from server.config import DATA_DIR
    expected = DATA_DIR / "models" / "faster-whisper"
    assert expected.exists()
    assert asr_engine._FASTER_WHISPER_CACHE_DIR == expected


def test_load_prefers_faster_whisper_over_sensevoice(monkeypatch):
    """验证 _load_asr_model 优先尝试 faster-whisper"""
    call_order = []

    class FakeWhisperModel:
        def __init__(self, model_name, device, compute_type, download_root):
            call_order.append("faster_whisper")

    # funasr 不可用时若先被调用会抛 ImportError，但 faster_whisper 应排在前面
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", type("M", (), {"WhisperModel": FakeWhisperModel}))
    asr_engine._asr_model = None
    asr_engine._asr_backend_type = None
    try:
        model, backend = asr_engine._load_asr_model()
        assert call_order[0] == "faster_whisper"
        assert backend == "faster_whisper"
    finally:
        asr_engine._asr_model = None
        asr_engine._asr_backend_type = None
