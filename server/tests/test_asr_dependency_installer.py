# -*- coding: utf-8 -*-
"""ASR 依赖自动安装管理器的单元测试"""
import time
from unittest.mock import patch

from server.core.audio.asr_dependency_installer import (
    AsrDependencyInstaller,
    get_asr_dependency_installer,
    maybe_start_auto_install,
)


def test_get_status_initial_state():
    inst = AsrDependencyInstaller()
    status = inst.get_status()
    assert status["state"] == "idle"
    assert status["is_busy"] is False
    assert status["package"] == "faster-whisper==1.1.1"


def test_is_installed_returns_bool_without_loading_model():
    inst = AsrDependencyInstaller()
    with patch(
        "server.core.audio.asr_engine.is_faster_whisper_installed",
        return_value=True,
    ):
        assert inst.is_installed() is True
    with patch(
        "server.core.audio.asr_engine.is_faster_whisper_installed",
        return_value=False,
    ):
        assert inst.is_installed() is False


def test_install_skipped_when_already_installed():
    inst = AsrDependencyInstaller()
    with patch.object(AsrDependencyInstaller, "is_installed", return_value=True):
        result = inst.start_install()
    assert result["ok"] is True
    assert result["reason"] == "already_installed"
    assert inst.get_status()["state"] == "installed"


def test_concurrent_install_rejected():
    inst = AsrDependencyInstaller()
    with patch.object(AsrDependencyInstaller, "is_installed", return_value=False):
        inst._state = "installing"
        result = inst.start_install()
    assert result["ok"] is False
    assert result["reason"] == "install_in_progress"
    inst._state = "idle"


def test_singleton_is_stable():
    a = get_asr_dependency_installer()
    b = get_asr_dependency_installer()
    assert a is b


def test_retry_cooldown_blocks_rapid_retrigger():
    """失败后冷却期内不再重复触发 pip 安装，避免体检轮询高频重试"""
    inst = AsrDependencyInstaller()
    with patch.object(AsrDependencyInstaller, "is_installed", return_value=False):
        inst._state = "failed"
        inst._finished_at = time.time()  # 刚刚失败
        assert inst.can_auto_retry() is False
        result = inst.start_install()
        assert result["ok"] is False
        assert result["reason"] == "retry_cooldown"

        # 冷却期过后允许重试
        inst._finished_at = time.time() - 120
        assert inst.can_auto_retry() is True
    inst._state = "idle"


def test_force_bypasses_cooldown():
    """用户手动重试接口可跳过冷却期"""
    inst = AsrDependencyInstaller()
    with patch.object(AsrDependencyInstaller, "is_installed", return_value=False):
        inst._state = "failed"
        inst._finished_at = time.time()
        result = inst.start_install(force=True)
        assert result["ok"] is True
        assert result["reason"] == "started"
    inst._state = "idle"


def test_maybe_start_auto_install_respects_global_gate(monkeypatch):
    """总开关关闭时 (测试/离线环境) 立即返回，不执行任何 pip 操作"""
    monkeypatch.setenv("LIVE_AGENT_DISABLE_AUTO_DOWNLOAD", "1")
    result = maybe_start_auto_install()
    assert result["ok"] is False
    assert result["reason"] == "auto_download_disabled"

    monkeypatch.setenv("LIVE_AGENT_DISABLE_AUTO_DOWNLOAD", "0")
    with patch.object(AsrDependencyInstaller, "is_installed", return_value=True):
        result = maybe_start_auto_install()
        assert result["ok"] is True
        assert result["reason"] == "already_installed"


def test_install_worker_records_failure_on_pip_error():
    """pip 子进程非零退出时记录 failed 状态与可读原因"""
    inst = AsrDependencyInstaller()

    class _FakeCompleted:
        returncode = 1
        stderr = "ERROR: Could not find a version that satisfies the requirement"

    with patch.object(AsrDependencyInstaller, "is_installed", return_value=False), patch(
        "server.core.audio.asr_dependency_installer.subprocess.run",
        return_value=_FakeCompleted(),
    ):
        inst._state = "idle"
        inst.start_install()
        for _ in range(120):
            if inst.get_status()["state"] == "failed":
                break
            time.sleep(0.05)
    status = inst.get_status()
    assert status["state"] == "failed"
    assert status["message"]
    inst._state = "idle"
