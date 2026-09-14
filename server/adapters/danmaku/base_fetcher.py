import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, Any


@dataclass
class FetcherHealth:
    """抓取器统一健康状态协议"""
    connected: bool = False
    worker_alive: bool = False
    last_heartbeat_at: Optional[float] = None
    reconnect_failures: int = 0
    last_error: Optional[str] = None
    last_event_at: Optional[float] = None


class BaseDanmakuFetcher(ABC):
    """直播间弹幕协议监听抽象基类"""
    def __init__(
        self,
        room_id: str,
        on_event_callback: Callable[[str, str, Dict[str, Any], int], Any],
        on_error_callback: Optional[Callable[[Exception, str], Any]] = None,
        on_health_change_callback: Optional[Callable[[FetcherHealth], Any]] = None,
    ):
        """
        :param room_id: 直播间房间号或 URL
        :param on_event_callback: 事件回调函数 (event_type, user_name, payload, priority)
        :param on_error_callback: 异常/断连错误回调函数 (exc, reason)
        :param on_health_change_callback: 健康状态变动回调
        """
        self.room_id = room_id
        self.on_event_callback = on_event_callback
        self.on_error_callback = on_error_callback
        self.on_health_change_callback = on_health_change_callback
        self.is_running = False
        self._health = FetcherHealth()

    def get_health(self) -> FetcherHealth:
        """获取当前抓取器链路健康状态快照"""
        return FetcherHealth(
            connected=self._health.connected,
            worker_alive=self._health.worker_alive and self.is_running,
            last_heartbeat_at=self._health.last_heartbeat_at,
            reconnect_failures=self._health.reconnect_failures,
            last_error=self._health.last_error,
            last_event_at=self._health.last_event_at,
        )

    def _trigger_health_update(self):
        cb = getattr(self, "on_health_change_callback", None)
        if cb:
            try:
                res = cb(self.get_health())
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                pass

    def on_connection_opened(self):
        """链路物理连接建立成功"""
        self._health.connected = True
        self._health.worker_alive = True
        self._health.reconnect_failures = 0
        self._health.last_error = None
        self._health.last_heartbeat_at = time.time()
        self._trigger_health_update()

    def on_heartbeat(self):
        """底层协议心跳包发送/响应成功"""
        self._health.last_heartbeat_at = time.time()
        self._health.connected = True
        self._health.worker_alive = True
        self._trigger_health_update()

    def on_connection_error(self, exc: Exception, reason: str = ""):
        """底层连接或协议解析异常时主动调用"""
        self._health.connected = False
        self._health.reconnect_failures += 1
        self._health.last_error = f"{reason}: {exc}" if reason else str(exc)
        self._trigger_health_update()

        cb = getattr(self, "on_error_callback", None)
        if cb:
            try:
                res = cb(exc, reason)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                pass

    def on_worker_stopped(self, error: Optional[Exception] = None):
        """后台 worker 协程停止或崩溃时主动调用"""
        self._health.worker_alive = False
        self._health.connected = False
        if error:
            self._health.last_error = str(error)
        self._trigger_health_update()

    def notify_error(self, exc: Exception, reason: str = ""):
        """兼容旧调用：底层连接或握手异常时主动上报"""
        self.on_connection_error(exc, reason)

    @abstractmethod
    async def start(self):
        """启动监听长连接"""
        pass

    @abstractmethod
    async def stop(self):
        """停止监听并断开连接"""
        pass
