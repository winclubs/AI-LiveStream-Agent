# -*- coding: utf-8 -*-
"""
server/core/monitoring/system_resource_watchdog.py
低配机 CPU/内存自适应降频看门狗与音频保活调度中枢 (任务 4)

【设计目标】
在轻薄本、核显或老旧 CPU 主机上开播时：
1. 动态监控宿主机 CPU 占用：当 CPU >= 85% 时，主动将渲染帧率由 25FPS 降频至 16FPS，
   释放 36% 运算时间片，全力保障 Edge-TTS / CosyVoice 语音合成与虚拟声卡播放；
2. 负荷恢复平稳 (连续 3 次采样 CPU < 70%) 时平滑恢复至基准 25FPS；
3. 内存巡检与自动 GC 回收：内存超限 (>= 88%) 或定时每 5 分钟执行 gc.collect()，
   杜绝 4 小时以上长时间开播的内存泄露与发烫降频；
4. 提供音频/核心工作进程优先级加固调度 (Windows HIGH_PRIORITY_CLASS)。
"""

import os
import gc
import sys
import time
import asyncio
import logging
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger("LiveAgent.SystemResourceWatchdog")

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    psutil = None
    PSUTIL_AVAILABLE = False


class SystemResourceWatchdog:
    """系统硬件资源自适应调节看门狗"""

    BASELINE_FPS = 25
    THROTTLED_FPS = 16
    CPU_OVERLOAD_THRESHOLD = 85.0   # 触发动态降频的 CPU 占用百分比
    CPU_RECOVERY_THRESHOLD = 70.0   # 触发平滑回升的安全 CPU 百分比
    RECOVERY_STABLE_CYCLES = 3       # 连续平稳采样次数（防抖）
    MEM_ALERT_THRESHOLD = 88.0       # 内存过高警戒线
    CHECK_INTERVAL_SEC = 3.0         # 巡检周期 (秒)
    GC_INTERVAL_SEC = 300.0          # 定时强制垃圾回收周期 (5分钟)

    def __init__(self, on_fps_change: Optional[Callable[[int], Any]] = None):
        self._task: Optional[asyncio.Task] = None
        self._on_fps_change = on_fps_change
        self.is_running = False

        # 状态指标
        self.current_fps = self.BASELINE_FPS
        self.is_throttled = False
        self.last_cpu_percent = 0.0
        self.last_mem_percent = 0.0
        self.throttle_count = 0
        self.recovery_count = 0
        self.last_gc_at = time.time()
        self._consecutive_low_cpu = 0

    def boost_audio_process_priority(self) -> bool:
        """提升当前进程与音频关键调度优先级 (优先保障音频不断音)"""
        if not PSUTIL_AVAILABLE or not psutil:
            return False
        try:
            p = psutil.Process()
            if sys.platform == "win32":
                # Windows 环境：提升为高于正常/高优先级
                p.nice(psutil.HIGH_PRIORITY_CLASS)
                logger.info("已将 Windows 音频与服务调度优先级提升至 HIGH_PRIORITY_CLASS")
                return True
            else:
                # Linux/macOS 环境：尝试调小 nice 值 (需权限)
                try:
                    p.nice(-5)
                except (PermissionError, psutil.AccessDenied):
                    pass
                return True
        except Exception as e:
            logger.debug(f"调整进程优先级提示 (非致命): {e}")
            return False

    def _sample_system_metrics(self) -> tuple[float, float]:
        """采样当前 CPU 与内存占用百分比"""
        if not PSUTIL_AVAILABLE or not psutil:
            return 0.0, 0.0
        try:
            # interval=None 为非阻塞即时估算
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            return float(cpu), float(mem)
        except Exception:
            return 0.0, 0.0

    async def _evaluate_and_adapt(self, cpu: float, mem: float):
        """核心动态自适应调频逻辑"""
        self.last_cpu_percent = cpu
        self.last_mem_percent = mem
        now = time.time()

        # 1. 内存过载或定时清理 (防止长时间开播内存碎片)
        if mem >= self.MEM_ALERT_THRESHOLD or (now - self.last_gc_at >= self.GC_INTERVAL_SEC):
            collected = gc.collect()
            self.last_gc_at = now
            if mem >= self.MEM_ALERT_THRESHOLD:
                logger.warning(f"系统内存占用达到 {mem:.1f}%，已触发深度 GC 垃圾回收 (清理对象数: {collected})")
            else:
                logger.debug(f"例行内存回收完成 (清理对象数: {collected})")

        # 2. CPU 负载判定与自适应降频/回升
        if cpu >= self.CPU_OVERLOAD_THRESHOLD:
            self._consecutive_low_cpu = 0
            if not self.is_throttled:
                # 触发自适应降频：25FPS -> 16FPS
                self.is_throttled = True
                self.current_fps = self.THROTTLED_FPS
                self.throttle_count += 1
                logger.warning(
                    f"⚠️ 宿主机 CPU 占用达到 {cpu:.1f}%（超过 {self.CPU_OVERLOAD_THRESHOLD}% 警戒线），"
                    f"已触发自适应降频机制 (FPS 降至 {self.THROTTLED_FPS})，全力确保音频算力充足！"
                )
                await self._notify_fps_change(self.THROTTLED_FPS)

        elif cpu < self.CPU_RECOVERY_THRESHOLD:
            if self.is_throttled:
                self._consecutive_low_cpu += 1
                if self._consecutive_low_cpu >= self.RECOVERY_STABLE_CYCLES:
                    # 连续稳定后平滑回升：16FPS -> 25FPS
                    self.is_throttled = False
                    self.current_fps = self.BASELINE_FPS
                    self.recovery_count += 1
                    self._consecutive_low_cpu = 0
                    logger.info(
                        f"✅ 宿主机 CPU 负荷已平稳回落至 {cpu:.1f}%，恢复基准全帧率 ({self.BASELINE_FPS} FPS)"
                    )
                    await self._notify_fps_change(self.BASELINE_FPS)
            else:
                self._consecutive_low_cpu = 0
        else:
            # 处于 70% ~ 85% 缓冲过渡区，保持当前状态
            self._consecutive_low_cpu = 0

    async def _notify_fps_change(self, fps: int):
        """下发帧率变动给媒体路由中枢"""
        if self._on_fps_change:
            try:
                res = self._on_fps_change(fps)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"下发动态自适应帧率失败: {e}")
        else:
            # 默认直通全局媒体中枢
            try:
                from server.adapters.media.media_router import global_media_router
                global_media_router.set_render_fps(fps)
            except Exception as e:
                logger.debug(f"直通媒体路由调整帧率提示: {e}")

    async def _loop(self):
        """看门狗后台常驻轮询协程"""
        # 初次采样作为基线
        if PSUTIL_AVAILABLE and psutil:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

        while self.is_running:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL_SEC)
                if not self.is_running:
                    break
                cpu, mem = self._sample_system_metrics()
                await self._evaluate_and_adapt(cpu, mem)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"资源看门狗巡检异常: {e}")

    def start(self):
        """开播时启动看门狗"""
        if self.is_running:
            return
        self.is_running = True
        self._consecutive_low_cpu = 0
        self.boost_audio_process_priority()
        self._task = asyncio.create_task(self._loop())
        logger.info("低配机 CPU/内存自适应降频看门狗已启动 (阈值: 85% 降频 / 70% 恢复)")

    async def stop(self):
        """停播时安全关闭并重置状态"""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        # 恢复默认帧率
        if self.is_throttled:
            self.is_throttled = False
            self.current_fps = self.BASELINE_FPS
            await self._notify_fps_change(self.BASELINE_FPS)
        logger.info("低配机 CPU/内存自适应降频看门狗已安全停止")

    def get_status(self) -> Dict[str, Any]:
        """获取资源看门狗实时健康诊断快照"""
        return {
            "is_running": self.is_running,
            "current_fps": self.current_fps,
            "baseline_fps": self.BASELINE_FPS,
            "throttled_fps": self.THROTTLED_FPS,
            "is_throttled": self.is_throttled,
            "last_cpu_percent": round(self.last_cpu_percent, 1),
            "last_mem_percent": round(self.last_mem_percent, 1),
            "throttle_count": self.throttle_count,
            "recovery_count": self.recovery_count,
            "overload_threshold": self.CPU_OVERLOAD_THRESHOLD,
            "recovery_threshold": self.CPU_RECOVERY_THRESHOLD,
        }


# 全局单例
global_resource_watchdog = SystemResourceWatchdog()
