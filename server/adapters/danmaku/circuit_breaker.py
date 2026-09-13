import asyncio
import logging
import time
from typing import Callable, Dict, Any, Optional
from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher
from server.adapters.danmaku.mock_fetcher import MockDanmakuFetcher

logger = logging.getLogger("LiveAgent.DanmakuCircuitBreaker")


class CircuitBreakerDanmakuFetcher(BaseDanmakuFetcher):
    """
    弹幕抓取熔断器代理类 (Circuit Breaker)
    - 状态机：CLOSED (正常转发真实数据) -> OPEN (连续失败触发熔断，降级为 Mock 兜底) -> HALF_OPEN (探活)
    - 现场保命：避免因第三方接口协议变动、网络中断导致整场直播控制器崩溃退播
    """

    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        real_fetcher: BaseDanmakuFetcher,
        room_id: str,
        on_event_callback: Callable[[str, str, Dict[str, Any], int], Any],
        on_state_change_callback: Optional[Callable[[str, str], Any]] = None,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 30.0,
    ):
        super().__init__(room_id=room_id, on_event_callback=on_event_callback)
        self.real_fetcher = real_fetcher
        self.on_state_change_callback = on_state_change_callback
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_sec = max(5.0, recovery_timeout_sec)

        self.state = self.STATE_CLOSED
        self.consecutive_failures = 0
        self.last_failure_time = 0.0
        self.last_state_change_time = time.time()
        self.is_running = False

        # 内部兜底 Mock 数据源
        self.mock_fetcher = MockDanmakuFetcher(
            room_id=room_id,
            on_event_callback=self._on_mock_event,
            auto_inject=True,
        )

        self._monitor_task: Optional[asyncio.Task] = None

        # 替换真实 fetcher 的回调，以便劫持并统计成功/失败事件
        if hasattr(self.real_fetcher, "on_event_callback"):
            self.real_fetcher.on_event_callback = self._on_real_event

    def _notify_state_change(self, new_state: str, reason: str):
        old_state = self.state
        self.state = new_state
        self.last_state_change_time = time.time()
        logger.warning(
            "【弹幕熔断器状态变更】%s -> %s，原因: %s (连续失败: %d)",
            old_state,
            new_state,
            reason,
            self.consecutive_failures,
        )
        if self.on_state_change_callback:
            try:
                res = self.on_state_change_callback(new_state, reason)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                logger.exception("调用熔断器状态变更回调失败")

    def _on_real_event(self, event_type: str, user_name: str, payload: Dict[str, Any], priority: int = 2):
        """真实数据源收到消息，说明通信正常"""
        if self.state in (self.STATE_OPEN, self.STATE_HALF_OPEN):
            self.consecutive_failures = 0
            self._notify_state_change(self.STATE_CLOSED, "真实弹幕源恢复通信，自动恢复正常流")
            # 停止 mock 兜底
            asyncio.create_task(self.mock_fetcher.stop())
        else:
            self.consecutive_failures = 0

        # 转发事件给上层直播控制器
        return self.on_event_callback(event_type, user_name, payload, priority)

    def _on_mock_event(self, event_type: str, user_name: str, payload: Dict[str, Any], priority: int = 2):
        """仅当熔断打开或半开且尚未接收到真实消息时，将 Mock 事件转交上层保持热场"""
        if self.state in (self.STATE_OPEN, self.STATE_HALF_OPEN):
            # 给 payload 打上降级标签
            payload["_is_fallback"] = True
            return self.on_event_callback(event_type, user_name, payload, priority)

    def record_failure(self, reason: str):
        """记录一次底层异常或断连"""
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        logger.warning(
            "真实弹幕源发生错误 (连续第 %d 次/%d): %s",
            self.consecutive_failures,
            self.failure_threshold,
            reason,
        )
        if self.consecutive_failures >= self.failure_threshold and self.state == self.STATE_CLOSED:
            self._notify_state_change(
                self.STATE_OPEN,
                f"真实弹幕源连续异常 {self.consecutive_failures} 次，自动熔断并降级为本地仿真互动",
            )
            # 启动 Mock 兜底
            asyncio.create_task(self.mock_fetcher.start())

    async def start(self):
        self.is_running = True
        self.consecutive_failures = 0
        self.state = self.STATE_CLOSED

        # 启动底层真实 fetcher
        try:
            await self.real_fetcher.start()
        except Exception as e:
            logger.exception("真实弹幕源初始启动失败，触发熔断器初次记录")
            self.record_failure(f"初次启动异常: {e}")

        # 启动后台自愈巡检任务
        self._monitor_task = asyncio.create_task(self._circuit_monitor_loop())
        logger.info("弹幕熔断器已启动，阈值=%d，探活周期=%.1fs", self.failure_threshold, self.recovery_timeout_sec)

    async def stop(self):
        self.is_running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        await self.real_fetcher.stop()
        await self.mock_fetcher.stop()
        logger.info("弹幕熔断器已停止")

    async def _circuit_monitor_loop(self):
        """熔断器探活循环：当处于 OPEN 状态超过 recovery_timeout_sec 时，尝试转为 HALF_OPEN"""
        while self.is_running:
            try:
                await asyncio.sleep(5.0)
                if not self.is_running:
                    break

                # 检查真实 fetcher 是否静默退出
                if hasattr(self.real_fetcher, "is_running") and not self.real_fetcher.is_running:
                    if self.state == self.STATE_CLOSED:
                        self.record_failure("底层连接已意外断开")

                if self.state == self.STATE_OPEN:
                    now = time.time()
                    if now - self.last_state_change_time >= self.recovery_timeout_sec:
                        self._notify_state_change(self.STATE_HALF_OPEN, "熔断冷却期结束，进入半开探测阶段")
                        # 尝试重启真实 fetcher
                        try:
                            await self.real_fetcher.start()
                        except Exception as e:
                            logger.warning("半开探测重启真实数据源失败: %s", e)
                            self.last_state_change_time = time.time()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("熔断器巡检循环异常")

    def get_status(self) -> dict:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "is_running": self.is_running,
            "is_fallback_active": self.state in (self.STATE_OPEN, self.STATE_HALF_OPEN),
        }
