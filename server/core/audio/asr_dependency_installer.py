# -*- coding: utf-8 -*-
"""
ASR 依赖自动安装管理器 (AsrDependencyInstaller)
- 体检/启动时检测到 faster-whisper 缺失即触发后台 pip 安装 (国内镜像源)；
- 状态机：idle -> pending -> installing -> installed / failed；
- 并发互斥：同一时刻只允许一个安装任务；
- 安装完成热生效：asr_engine 下次懒加载自动走 faster-whisper，无需重启服务；
- 自动重试冷却：失败后冷却期内不重复触发，避免体检轮询高频反复安装。
"""
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

from server.config import DEFAULT_PIP_INDEX, auto_download_enabled

logger = logging.getLogger("LiveAgent.AsrDependencyInstaller")

_PACKAGE_NAME = "faster-whisper"
# 与 server/requirements-optional.txt 的锁定版本保持一致，杜绝版本漂移
_PACKAGE_SPEC = "faster-whisper==1.1.1"

# 失败后自动重试冷却秒数 (避免体检轮询高频触发 pip 安装)
_RETRY_COOLDOWN_SEC = 60.0


class AsrDependencyInstaller:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: str = "idle"          # idle / pending / installing / installed / failed
        self._progress: int = 0
        self._message: str = ""
        self._started_at: float = 0.0
        self._finished_at: float = 0.0
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "package": _PACKAGE_SPEC,
            "progress": self._progress,
            "message": self._message,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "is_busy": self._state in ("pending", "installing"),
            "installed": self.is_installed(),
        }

    @staticmethod
    def is_installed() -> bool:
        """检测 faster-whisper 是否已安装 (委托 asr_engine 的轻量探测，不触发模型加载)"""
        try:
            from server.core.audio.asr_engine import is_faster_whisper_installed
            return is_faster_whisper_installed()
        except Exception as e:
            logger.debug(f"检测 faster-whisper 安装状态异常: {e}")
            return False

    def can_auto_retry(self) -> bool:
        """失败后是否已过冷却期，可再次自动触发安装"""
        if self._state != "failed":
            return True
        if self._finished_at == 0.0:
            return True
        return (time.time() - self._finished_at) >= _RETRY_COOLDOWN_SEC

    # ------------------------------------------------------------------
    # 安装触发
    # ------------------------------------------------------------------
    def start_install(self, force: bool = False) -> Dict[str, Any]:
        """
        触发后台 pip 安装 (已安装则直接返回就绪)。返回操作结果与当前状态。

        - force=True 时跳过冷却期检查 (供用户手动重试接口使用)。
        """
        with self._lock:
            if self._state in ("pending", "installing"):
                return {"ok": False, "reason": "install_in_progress", "status": self.get_status()}
            if self.is_installed():
                self._set_state("installed", 100, "faster-whisper 已安装，无需重复安装")
                return {"ok": True, "reason": "already_installed", "status": self.get_status()}
            if not force and not self.can_auto_retry():
                return {"ok": False, "reason": "retry_cooldown", "status": self.get_status()}

            self._set_state("pending", 0, "已加入自动安装队列")

        self._worker = threading.Thread(
            target=self._install_worker,
            name="AsrDependencyInstall",
            daemon=True,
        )
        self._worker.start()
        return {"ok": True, "reason": "started", "status": self.get_status()}

    def _set_state(self, state: str, progress: int = 0, message: str = "") -> None:
        self._state = state
        self._progress = int(progress)
        self._message = message
        if state in ("pending", "installing") and self._started_at == 0.0:
            self._started_at = time.time()
        if state in ("installed", "failed"):
            self._finished_at = time.time()

    def _install_worker(self) -> None:
        try:
            if self.is_installed():
                with self._lock:
                    self._set_state("installed", 100, "faster-whisper 已安装")
                return

            with self._lock:
                self._set_state("installing", 5, "正在连接镜像源并解析依赖...")

            # 同步安装 faster-whisper (约30MB，无 torch 依赖)
            ok = self._run_pip_install()

            with self._lock:
                if ok and self.is_installed():
                    self._set_state(
                        "installed", 100,
                        "faster-whisper 安装完成，首次语音输入时自动懒加载轻量转写引擎",
                    )
                    logger.info("ASR 依赖 faster-whisper 自动安装完成，asr_engine 将在下次懒加载时生效")
                else:
                    self._set_state(
                        "failed", 0,
                        "pip 安装失败，请检查网络或镜像源可访问性" if not ok else "pip 安装结束但未检测到 faster-whisper",
                    )
        except Exception as e:
            logger.exception("ASR 依赖后台自动安装异常")
            with self._lock:
                self._set_state("failed", 0, f"自动安装异常: {e}")

    def _run_pip_install(self) -> bool:
        """在子进程中执行 pip 安装，周期性把 pip 的下载进度换算为百分比"""
        cmd = [
            sys.executable, "-m", "pip", "install",
            "-i", DEFAULT_PIP_INDEX,
            "--disable-pip-version-check",
            "--no-input",
            _PACKAGE_SPEC,
        ]

        # Windows 下隐藏可能弹出的控制台子窗口
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        stop_flag = threading.Event()

        def monitor() -> None:
            """基于已用时长做粗粒度进度推进 (pip 无标准化的进度回调)"""
            elapsed_cap = 90.0
            while not stop_flag.is_set():
                try:
                    if self._state == "installing":
                        elapsed = time.time() - max(self._started_at, 1.0)
                        # 下载+解析阶段最多推进到 90%，剩余 10% 留给安装收尾
                        pct = max(5, min(90, int(5 + (elapsed / elapsed_cap) * 85)))
                        with self._lock:
                            if self._state == "installing":
                                self._progress = pct
                                self._message = f"正在下载并安装 faster-whisper 依赖包 ({pct}%)"
                except Exception:
                    pass
                stop_flag.wait(1.0)

        mon_thread = threading.Thread(target=monitor, name="AsrInstallMonitor", daemon=True)
        mon_thread.start()

        try:
            logger.info(f"开始后台安装 ASR 依赖: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=600,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                err_tail = (result.stderr or "").strip().splitlines()
                tail = err_tail[-1] if err_tail else f"pip 退出码 {result.returncode}"
                logger.warning(f"pip 安装 faster-whisper 失败: {tail}")
                with self._lock:
                    self._message = f"pip 安装失败: {tail}"
            return result.returncode == 0
        finally:
            stop_flag.set()


# 全局单例
_global_asr_dependency_installer: Optional[AsrDependencyInstaller] = None
_singleton_lock = threading.Lock()


def get_asr_dependency_installer() -> AsrDependencyInstaller:
    global _global_asr_dependency_installer
    if _global_asr_dependency_installer is None:
        with _singleton_lock:
            if _global_asr_dependency_installer is None:
                _global_asr_dependency_installer = AsrDependencyInstaller()
    return _global_asr_dependency_installer


def maybe_start_auto_install() -> Dict[str, Any]:
    """
    供启动/体检调用的安全入口：仅在自动下载总开关开启且依赖缺失时触发后台安装。

    总开关关闭时 (测试/离线环境) 立即返回，不执行任何 pip 操作。
    """
    if not auto_download_enabled():
        return {"ok": False, "reason": "auto_download_disabled", "status": None}
    return get_asr_dependency_installer().start_install()
