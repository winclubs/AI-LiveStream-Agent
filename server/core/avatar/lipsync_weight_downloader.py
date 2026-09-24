# -*- coding: utf-8 -*-
"""
神经唇形权重按需下载管理器 (LipSyncWeightDownloader)
- 体检时检测到 onnx_lipsync.onnx 缺失即触发后台下载 (ModelScope 优先)；
- 复用 scripts/download_weights.py 的多源流式下载与原子重命名逻辑，杜绝双份维护；
- 状态机：pending -> downloading -> installed / failed；
- 并发互斥：同一时刻只允许一个下载任务；
- 下载完成热挂载：通知 NeuralLipRenderer 下次渲染自动走真实推理。
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("LiveAgent.LipSyncWeightDownloader")

_MODEL_KEY = "onnx_lipsync"


class LipSyncWeightDownloader:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: str = "idle"          # idle / pending / downloading / installed / failed
        self._task_id: str = ""
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
            "task_id": self._task_id,
            "progress": self._progress,
            "message": self._message,
            "model_key": _MODEL_KEY,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "is_busy": self._state in ("pending", "downloading"),
        }

    def is_weight_ready(self) -> bool:
        """检测神经唇形权重是否已就绪 (委托 NeuralModelManager 单一真相源)"""
        try:
            from server.core.avatar.neural_model_manager import global_neural_model_manager
            return bool(global_neural_model_manager.is_model_available(_MODEL_KEY))
        except Exception as e:
            logger.debug(f"检测神经唇形权重可用性异常: {e}")
            return False

    # ------------------------------------------------------------------
    # 下载触发
    # ------------------------------------------------------------------
    def start_download(self, source: str = "modelscope", task_id: str = "") -> Dict[str, Any]:
        """
        触发后台下载 (已在则直接返回就绪)。返回操作结果与当前状态。
        """
        with self._lock:
            if self._state in ("pending", "downloading"):
                return {"ok": False, "reason": "download_in_progress", "status": self.get_status()}
            if self.is_weight_ready():
                self._set_state("installed", 100, "神经唇形权重已就绪，无需下载")
                return {"ok": True, "reason": "already_installed", "status": self.get_status()}

            self._set_state("pending", 0, "已加入下载队列", task_id=task_id)

        self._worker = threading.Thread(
            target=self._download_worker,
            args=(source,),
            name="LipSyncWeightDownload",
            daemon=True,
        )
        self._worker.start()
        return {"ok": True, "reason": "started", "status": self.get_status()}

    def _set_state(
        self,
        state: str,
        progress: int = 0,
        message: str = "",
        task_id: Optional[str] = None,
    ) -> None:
        self._state = state
        self._progress = int(progress)
        self._message = message
        if task_id is not None:
            self._task_id = task_id
        if state == "downloading" and self._started_at == 0.0:
            self._started_at = time.time()
        if state in ("installed", "failed"):
            self._finished_at = time.time()

    def _download_worker(self, source: str) -> None:
        try:
            from server.core.avatar.neural_model_manager import global_neural_model_manager
            model_path = global_neural_model_manager.find_model_path(_MODEL_KEY)
            if model_path and Path(model_path).exists():
                with self._lock:
                    self._set_state("installed", 100, "神经唇形权重已就绪")
                return

            # 复用 scripts/download_weights.py 的下载能力 (保持单一真相源)
            import importlib.util
            import sys

            script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "download_weights.py"
            if not script_path.exists():
                with self._lock:
                    self._set_state("failed", 0, f"下载脚本不存在: {script_path}")
                return

            spec = importlib.util.spec_from_file_location("download_weights_mod", script_path)
            if spec is None or spec.loader is None:
                with self._lock:
                    self._set_state("failed", 0, "无法加载下载脚本模块")
                return
            mod = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("download_weights_mod", mod)
            spec.loader.exec_module(mod)

            cfg = mod.MODELS_CONFIG.get("onnx-lipsync")
            if not cfg:
                with self._lock:
                    self._set_state("failed", 0, "下载脚本缺少 onnx-lipsync 配置")
                return

            with self._lock:
                self._set_state("downloading", 5, "正在连接下载源 (ModelScope 优先)...")

            ok = self._run_with_progress(mod, cfg, source)

            with self._lock:
                if ok:
                    self._set_state("installed", 100, "神经唇形权重下载完成，NeuralLipRenderer 将自动加载真实推理")
                    logger.info("神经唇形权重按需下载完成并已热挂载")
                else:
                    self._set_state("failed", 0, "全部下载源均不可用，可稍后重试或检查网络")
        except Exception as e:
            logger.exception("神经唇形权重后台下载异常")
            with self._lock:
                self._set_state("failed", 0, f"下载异常: {e}")

    def _run_with_progress(self, mod: Any, cfg: Dict[str, Any], source: str) -> bool:
        """
        在子线程内运行单文件下载，并周期性把文件增长换算为进度。
        下载器本身是同步阻塞的，这里用目标文件体积做粗粒度进度估算。
        """
        target_path = Path(cfg["target_dir"]) / cfg["filename"]
        total_expected_mb = float(cfg.get("expected_mb") or 45.0)

        # 启动一个监控线程更新进度 (基于已落盘的 .part 文件体积)
        stop_flag = threading.Event()

        def monitor():
            while not stop_flag.is_set():
                try:
                    part = target_path.with_suffix(target_path.suffix + ".part")
                    if part.exists():
                        done_mb = part.stat().st_size / (1024 * 1024)
                        pct = max(5, min(99, int(done_mb / max(total_expected_mb, 1.0) * 100)))
                        with self._lock:
                            if self._state == "downloading":
                                self._progress = pct
                                self._message = f"正在下载神经唇形权重 ({done_mb:.1f}MB / {total_expected_mb:.0f}MB)"
                except Exception:
                    pass
                stop_flag.wait(0.5)

        mon_thread = threading.Thread(target=monitor, name="LipSyncDownloadMonitor", daemon=True)
        mon_thread.start()

        try:
            urls = mod._resolve_single_file_urls(cfg, source)
            if not urls:
                return False
            return mod._download_single_file(urls, target_path)
        finally:
            stop_flag.set()


# 全局单例
_global_lipsync_weight_downloader: Optional[LipSyncWeightDownloader] = None
_singleton_lock = threading.Lock()


def get_lipsync_weight_downloader() -> LipSyncWeightDownloader:
    global _global_lipsync_weight_downloader
    if _global_lipsync_weight_downloader is None:
        with _singleton_lock:
            if _global_lipsync_weight_downloader is None:
                _global_lipsync_weight_downloader = LipSyncWeightDownloader()
    return _global_lipsync_weight_downloader
