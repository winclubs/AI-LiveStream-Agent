# -*- coding: utf-8 -*-
"""神经唇形权重按需下载管理器的单元测试"""
import threading
import time
from pathlib import Path
from unittest.mock import patch

from server.core.avatar.lipsync_weight_downloader import (
    LipSyncWeightDownloader,
    get_lipsync_weight_downloader,
)


def test_get_status_initial_state():
    dl = LipSyncWeightDownloader()
    status = dl.get_status()
    assert status["state"] == "idle"
    assert status["is_busy"] is False
    assert status["model_key"] == "onnx_lipsync"


def test_weight_ready_uses_neural_model_manager():
    dl = LipSyncWeightDownloader()
    with patch(
        "server.core.avatar.neural_model_manager.global_neural_model_manager.is_model_available",
        return_value=True,
    ):
        assert dl.is_weight_ready() is True
    with patch(
        "server.core.avatar.neural_model_manager.global_neural_model_manager.is_model_available",
        return_value=False,
    ):
        assert dl.is_weight_ready() is False


def test_download_skipped_when_already_installed():
    dl = LipSyncWeightDownloader()
    with patch.object(LipSyncWeightDownloader, "is_weight_ready", return_value=True):
        result = dl.start_download()
    assert result["ok"] is True
    assert result["reason"] == "already_installed"
    assert dl.get_status()["state"] == "installed"


def test_concurrent_download_rejected():
    dl = LipSyncWeightDownloader()
    with patch.object(LipSyncWeightDownloader, "is_weight_ready", return_value=False):
        # 先模拟占用状态
        dl._state = "downloading"
        result = dl.start_download()
    assert result["ok"] is False
    assert result["reason"] == "download_in_progress"
    dl._state = "idle"


def test_singleton_is_stable():
    a = get_lipsync_weight_downloader()
    b = get_lipsync_weight_downloader()
    assert a is b


def test_failure_state_records_message():
    dl = LipSyncWeightDownloader()
    # 让 worker 内部依赖的脚本加载失败，走 except 分支记录 failed 状态
    with patch.object(LipSyncWeightDownloader, "is_weight_ready", return_value=False), patch(
        "pathlib.Path.exists",
        return_value=False,
    ):
        dl._state = "idle"
        dl.start_download()
        # 等待 worker 异常落地
        for _ in range(60):
            if dl.get_status()["state"] == "failed":
                break
            time.sleep(0.05)
    status = dl.get_status()
    assert status["state"] == "failed"
    assert status["message"]  # 有具体失败原因
