"""
显存溢出 (OOM) 熔断与动态降级看门狗 (规划 §15.3)
- 每 10 秒巡检 GPU 显存占用
- 超过 92% 警戒阈值时清空 PyTorch CUDA 缓存池并广播告警
- 无 NVIDIA 环境 (CPU 模式/未装 torch) 自动空转，零开销
"""
import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("LiveAgent.VRAMWatchdog")

VRAM_ALERT_THRESHOLD = 0.92
CHECK_INTERVAL_SEC = 10.0


class VRAMWatchdog:
    def __init__(self, on_alert: Optional[Callable[[dict], Awaitable[None]]] = None):
        self._task: Optional[asyncio.Task] = None
        self._on_alert = on_alert
        self.last_alert_at = 0.0

    def _probe_vram(self) -> Optional[float]:
        """返回显存占用率 (0~1)，无 GPU 环境返回 None"""
        try:
            import importlib
            torch = importlib.import_module("torch")
            if not torch.cuda.is_available():
                return None
            used, total = torch.cuda.mem_get_info(0)
            return 1.0 - (used / total)
        except Exception:
            return None

    async def _loop(self):
        import time
        while True:
            try:
                usage = self._probe_vram()
                if usage is not None and usage >= VRAM_ALERT_THRESHOLD:
                    # 熔断保护：清空 CUDA 缓存池
                    try:
                        import importlib
                        torch = importlib.import_module("torch")
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    now = time.time()
                    if now - self.last_alert_at > 30:  # 告警节流，30 秒最多一次
                        self.last_alert_at = now
                        logger.warning(f"显存占用率 {usage:.1%} 超过 {VRAM_ALERT_THRESHOLD:.0%} 警戒线，已触发 OOM 熔断保护")
                        if self._on_alert:
                            await self._on_alert({
                                "usage": round(usage, 4),
                                "action": "cuda_cache_cleared",
                            })
                await asyncio.sleep(CHECK_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"VRAM 巡检异常: {e}")
                await asyncio.sleep(CHECK_INTERVAL_SEC)

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("VRAM 看门狗已启动 (阈值 92%)")

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
