import asyncio
import logging
import time
from typing import Callable, Dict, Any, Optional
from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher, FetcherHealth
from server.adapters.danmaku.mock_fetcher import MockDanmakuFetcher

logger = logging.getLogger("LiveAgent.DanmakuCircuitBreaker")

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_DEGRADED = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"


class CircuitBreakerDanmakuFetcher(BaseDanmakuFetcher):
    """
    弹幕抓取熔断器代理类 (Circuit Breaker)
    - 状态机：
        CLOSED: 正常转发真实数据
        DEGRADED (OPEN): 真实链路断开或心跳超时，标记降级、报警，进入商品轮播/冷场垫场，绝不在正式直播注入假弹幕
        HALF_OPEN: 探活阶段，尝试重建真实连接
    - 统一健康协议：监听底层协议心跳包、WebSocket 连接状态与连续重试次数
    """

    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_DEGRADED = "OPEN"  # 保持 DEGRADED 与 OPEN 等价别名
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        real_fetcher: BaseDanmakuFetcher,
        room_id: str,
        on_event_callback: Callable[[str, str, Dict[str, Any], int], Any],
        on_state_change_callback: Optional[Callable[[str, str], Any]] = None,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 30.0,
        enable_mock_fallback: bool = True,
    ):
        super().__init__(room_id=room_id, on_event_callback=on_event_callback)
        self.real_fetcher = real_fetcher
        self.on_state_change_callback = on_state_change_callback
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_sec = max(0.1, recovery_timeout_sec)
        self.enable_mock_fallback = enable_mock_fallback

        self.state = self.STATE_CLOSED
        self.consecutive_failures = 0
        self.last_failure_time = 0.0
        self.last_state_change_time = time.time()
        self.is_running = False

        # 仅在明确开启仿真兜底时（演示模式）使用 Mock，且必须携带 is_mock 标签
        self.mock_fetcher = MockDanmakuFetcher(
            room_id=room_id,
            on_event_callback=self._on_mock_event,
            auto_inject=enable_mock_fallback,
        )

        self._monitor_task: Optional[asyncio.Task] = None
        self.last_real_event_time = time.time()
        self.heartbeat_timeout_sec = 45.0
        self._is_unhealthy_edge = False

        # 劫持并替换真实 fetcher 的回调，以便统计成功/失败事件及链路错误
        if hasattr(self.real_fetcher, "on_event_callback"):
            self.real_fetcher.on_event_callback = self._on_real_event
        if hasattr(self.real_fetcher, "on_error_callback"):
            self.real_fetcher.on_error_callback = self._on_underlying_error
        if hasattr(self.real_fetcher, "on_health_change_callback"):
            self.real_fetcher.on_health_change_callback = self._on_underlying_health_change

    def _on_underlying_health_change(self, health: FetcherHealth):
        """响应底层抓取器上报的真实健康协议 (仅同步状态与探活恢复，不重复调用 record_failure 避免双重计次)"""
        self._health = health
        if health.connected and health.worker_alive:
            self._is_unhealthy_edge = False
            self.consecutive_failures = 0
            if self.state != self.STATE_CLOSED:
                self._notify_state_change(self.STATE_CLOSED, "底层健康协议检测到连接与心跳已恢复")

    def _on_underlying_error(self, exc: Exception, reason: str = ""):
        """底层抓取器连接中断/重连时触发，直接计入熔断器失败 (单点权威记录)"""
        self._is_unhealthy_edge = True
        self.record_failure(f"底层数据源异常 [{reason}]: {exc}")

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
        self.last_real_event_time = time.time()
        self._is_unhealthy_edge = False
        if self.state in (self.STATE_OPEN, self.STATE_HALF_OPEN):
            self.consecutive_failures = 0
            self._notify_state_change(self.STATE_CLOSED, "真实弹幕源恢复通信，自动恢复正常流")
            if self.enable_mock_fallback:
                asyncio.create_task(self.mock_fetcher.stop())
        else:
            self.consecutive_failures = 0

        # 转发事件给上层直播控制器
        return self.on_event_callback(event_type, user_name, payload, priority)

    def _on_mock_event(self, event_type: str, user_name: str, payload: Dict[str, Any], priority: int = 2):
        """仅在用户明确开启了仿真兜底（如演示环境）时，才转发 Mock 事件并强制标记 is_mock"""
        if self.enable_mock_fallback and self.state in (self.STATE_OPEN, self.STATE_HALF_OPEN):
            payload["_is_mock"] = True
            payload["_is_fallback"] = True
            return self.on_event_callback(event_type, user_name, payload, priority)

    def record_failure(self, reason: str = ""):
        """记录一次底层连接/心跳失败，并驱动状态机转换"""
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        logger.warning(
            "真实弹幕源发生错误 (连续第 %d 次/%d): %s",
            self.consecutive_failures,
            self.failure_threshold,
            reason,
        )
        should_open = (
            (self.consecutive_failures >= self.failure_threshold and self.state == self.STATE_CLOSED)
            or self.state == self.STATE_HALF_OPEN
        )
        if should_open:
            self._notify_state_change(
                self.STATE_OPEN,
                f"真实弹幕源异常 (连续第 {self.consecutive_failures} 次)，进入降级熔断状态: {reason}",
            )
            if self.enable_mock_fallback:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.mock_fetcher.start())
                except RuntimeError:
                    pass

    async def start(self):
        self.is_running = True
        self.consecutive_failures = 0
        self.state = self.STATE_CLOSED
        self.last_real_event_time = time.time()

        # 启动底层真实 fetcher
        try:
            await self.real_fetcher.start()
        except Exception as e:
            logger.exception("真实弹幕源初始启动失败，触发熔断器初次记录")
            self.record_failure(f"初次启动异常: {e}")

        # 启动后台自愈巡检任务
        self._monitor_task = asyncio.create_task(self._circuit_monitor_loop())
        logger.info("弹幕熔断器已启动，阈值=%d，探活周期=%.1fs，仿真兜底=%s", self.failure_threshold, self.recovery_timeout_sec, self.enable_mock_fallback)

    async def stop(self):
        self.is_running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        try:
            await self.real_fetcher.stop()
        except Exception as exc:
            logger.warning("停止真实抓取器异常: %s", exc)
        try:
            if self.enable_mock_fallback:
                await self.mock_fetcher.stop()
        except Exception as exc:
            logger.warning("停止 Mock 抓取器异常: %s", exc)
        logger.info("弹幕熔断器已停止")

    async def _circuit_monitor_loop(self):
        """熔断器探活循环：当处于 DEGRADED 状态超过 recovery_timeout_sec 时，尝试转为 HALF_OPEN 并探活"""
        while self.is_running:
            try:
                await asyncio.sleep(5.0)
                if not self.is_running:
                    break

                # 检查真实 fetcher 健康状态协议
                health = self.real_fetcher.get_health() if hasattr(self.real_fetcher, "get_health") else None
                if health:
                    is_down = False
                    reason = ""
                    now = time.time()

                    if not health.worker_alive or not health.connected:
                        is_down = True
                        reason = f"健康协议检测到断联: {health.last_error or '连接断开'}"
                    elif health.last_heartbeat_at > 0 and (now - health.last_heartbeat_at) > self.heartbeat_timeout_sec:
                        is_down = True
                        reason = f"心跳超时 (距离上次心跳已过去 {int(now - health.last_heartbeat_at)} 秒)"

                    if is_down:
                        # 边沿触发：仅在从正常状态转为故障状态时计入一次失败，持续故障期间不重复累加
                        if not self._is_unhealthy_edge:
                            self._is_unhealthy_edge = True
                            if self.state == self.STATE_CLOSED:
                                self.record_failure(reason)
                    else:
                        self._is_unhealthy_edge = False

                if self.state == self.STATE_DEGRADED:
                    now = time.time()
                    if now - self.last_state_change_time >= self.recovery_timeout_sec:
                        self._notify_state_change(self.STATE_HALF_OPEN, "熔断冷却期结束，进入半开探测阶段")
                        try:
                            await self.real_fetcher.stop()
                        except Exception as stop_err:
                            logger.debug("半开探测前清理旧抓取器任务: %s", stop_err)

                        try:
                            await self.real_fetcher.start()
                        except Exception as e:
                            logger.warning("半开探测重启真实数据源失败: %s", e)
                            self.last_state_change_time = time.time()
                            self.record_failure(f"半开探测重启失败: {e}")
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
            "is_degraded": self.state in (self.STATE_DEGRADED, self.STATE_HALF_OPEN),
            "enable_mock_fallback": self.enable_mock_fallback,
            "last_real_event_time": self.last_real_event_time,
            "health": self.real_fetcher.get_health().__dict__ if hasattr(self.real_fetcher, "get_health") else None,
        }
