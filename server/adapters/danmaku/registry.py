"""
弹幕抓取器平台注册表 (插件化架构)
- 内置 Bilibili 真实协议；其他平台协议可由第三方以插件形式注册 (register)
- 未注册的平台统一回退到外部中继 (webhook / WS ingest) 或仿真注入器
"""
import logging
from typing import Callable, Dict, Optional, Any

from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher

logger = logging.getLogger("LiveAgent.DanmakuRegistry")

FetcherFactory = Callable[..., BaseDanmakuFetcher]


class DanmakuFetcherRegistry:
    def __init__(self):
        self._factories: Dict[str, FetcherFactory] = {}

    def register(self, platform: str, factory: FetcherFactory):
        self._factories[platform.lower().strip()] = factory

    def has(self, platform: str) -> bool:
        return (platform or "").lower().strip() in self._factories

    def list_platforms(self) -> list:
        return sorted(self._factories.keys())

    def create(self, platform: str, room_id: str, on_event_callback, **kwargs) -> Optional[BaseDanmakuFetcher]:
        key = (platform or "").lower().strip()
        factory = self._factories.get(key)
        if not factory:
            return None
        try:
            return factory(room_id=room_id, on_event_callback=on_event_callback, **kwargs)
        except Exception as e:
            logger.error(f"创建 {platform} 弹幕抓取器失败: {e}")
            return None


global_danmaku_registry = DanmakuFetcherRegistry()


def _register_builtin():
    from server.adapters.danmaku.bilibili_fetcher import BilibiliDanmakuFetcher
    from server.adapters.danmaku.douyin_fetcher import DouyinDanmakuFetcher

    global_danmaku_registry.register(
        "bilibili",
        lambda room_id, on_event_callback, **kw: BilibiliDanmakuFetcher(room_id, on_event_callback),
    )
    global_danmaku_registry.register(
        "douyin",
        lambda room_id, on_event_callback, **kw: DouyinDanmakuFetcher(room_id, on_event_callback, **kw),
    )


_register_builtin()

