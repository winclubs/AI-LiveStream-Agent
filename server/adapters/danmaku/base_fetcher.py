import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Optional, Dict, Any

class BaseDanmakuFetcher(ABC):
    """直播间弹幕协议监听抽象基类"""
    def __init__(self, room_id: str, on_event_callback: Callable[[str, str, Dict[str, Any], int], Any]):
        """
        :param room_id: 直播间房间号或 URL
        :param on_event_callback: 事件回调函数 (event_type, user_name, payload, priority)
        """
        self.room_id = room_id
        self.on_event_callback = on_event_callback
        self.is_running = False

    @abstractmethod
    async def start(self):
        """启动监听长连接"""
        pass

    @abstractmethod
    async def stop(self):
        """停止监听并断开连接"""
        pass
