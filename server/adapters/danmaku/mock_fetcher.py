import asyncio
import time
from typing import Callable, Dict, Any, Optional
from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher

class MockDanmakuFetcher(BaseDanmakuFetcher):
    """
    仿真弹幕注入器，用于离线本地开发、高并发测试与功能验证
    auto_inject=False 时为被动模式 (仅供未注册平台的通用中继场景挂载，
    不注入任何仿真事件，弹幕完全依赖 webhook / WS-ingest 外部推送)
    """
    def __init__(self, room_id: str, on_event_callback: Callable[[str, str, Dict[str, Any], int], Any], auto_inject: bool = True):
        super().__init__(room_id, on_event_callback)
        self._task: Optional[asyncio.Task] = None
        self.auto_inject = auto_inject

    async def start(self):
        self.is_running = True
        if self.auto_inject:
            self._task = asyncio.create_task(self._simulation_loop())

    async def stop(self):
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def simulate_event(self, event_type: str, user_name: str, payload: Dict[str, Any], priority: int = 2):
        """手动注入一条明确标记为 Mock 的弹幕或礼物事件。"""
        if self.on_event_callback:
            mock_payload = dict(payload)
            mock_payload["_is_mock"] = True
            res = self.on_event_callback(event_type, user_name, mock_payload, priority)
            if asyncio.iscoroutine(res):
                await res

    async def _simulation_loop(self):
        try:
            # 启动时先打一条进房通知
            await asyncio.sleep(0.5)
            await self.simulate_event("follow", "数码达人小李", {}, priority=2)

            while self.is_running:
                await asyncio.sleep(8.0)
                if not self.is_running:
                    break
                # 定期模拟一条普通咨询弹幕
                await self.simulate_event("chat", "路人张三", {"text": "主播这款发货快吗？"}, priority=1)
        except asyncio.CancelledError:
            pass
