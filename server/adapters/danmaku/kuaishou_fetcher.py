# -*- coding: utf-8 -*-
"""
快手直播间弹幕监听适配器 (KuaishouDanmakuFetcher)
支持快手 Web 端长轮询与长连接信令解析，提供高意图促单提权与大额礼物 P0 打断。
遵循 ADR-16 诚实契约与统一抓取器健康状态机 (BaseDanmakuFetcher)。
"""

import re
import time
import asyncio
import logging
from typing import Callable, Dict, Any, Optional

from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher

logger = logging.getLogger("LiveAgent.KuaishouFetcher")

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


class KuaishouDanmakuFetcher(BaseDanmakuFetcher):
    """
    快手直播间实时弹幕抓取适配器
    支持事件：
      - chat: 观众发言（含电商促单意图升级 P1）
      - gift: 礼物打赏（大额礼物如穿云箭/火箭 >= 50000 币升级 P0 强打断）
      - like: 点赞互动 (P2)
      - member: 进场欢迎 (P2)
    """
    platform_name = "kuaishou"

    # 促单高意图关键词
    INTENT_KEYWORDS = ["多少钱", "怎么买", "发货", "优惠", "包邮", "领券", "几号链接", "库存", "正品", "拍了", "保修"]

    # P0 强打断礼物金币阈值 (50000 币)
    P0_GIFT_COIN_THRESHOLD = 50000

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    def __init__(
        self,
        room_id: str,
        on_event_callback: Callable[[str, str, Dict[str, Any], int], Any],
        cookie: str = "",
        poll_interval: float = 2.0,
        **kwargs
    ):
        super().__init__(room_id, on_event_callback)
        self.clean_room_id = self._extract_room_id(room_id)
        self.cookie = cookie or ""
        self.poll_interval = max(1.0, float(poll_interval))
        self.worker_task: Optional[asyncio.Task] = None
        self._cursor: str = ""
        self.watched_count: int = 0

    @staticmethod
    def _extract_room_id(raw_input: str) -> str:
        """从链接或字符串中提取快手直播间 ID / 作者 ID"""
        if not raw_input:
            return ""
        s = str(raw_input).strip()
        # 例如: https://live.kuaishou.com/u/3x999999 或 /live/123456
        m = re.search(r"live\.kuaishou\.com/(?:u|live)/([a-zA-Z0-9_\-]+)", s)
        if m:
            return m.group(1)
        # 纯数字或作者标识
        matched = re.findall(r"[a-zA-Z0-9_\-]+", s)
        return matched[0] if matched else s

    async def start(self):
        """启动监听工作协程"""
        self.is_running = True
        self.worker_task = asyncio.create_task(self._listen_loop())
        logger.info(f"快手弹幕监听器已就绪，目标直播间: {self.clean_room_id or self.room_id}")

    async def stop(self):
        """停止监听并清理协程"""
        self.is_running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        self.on_worker_stopped()
        logger.info(f"快手直播间 {self.clean_room_id} 弹幕监听已安全停止")

    async def _listen_loop(self):
        """核心监听与断线指数退避重试循环"""
        retry_delay = 1.0
        max_delay = 16.0

        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": f"https://live.kuaishou.com/u/{self.clean_room_id}",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        while self.is_running:
            self.on_connection_attempted()
            try:
                if not httpx:
                    raise RuntimeError("未安装 httpx 依赖库，无法执行快手网络请求")

                async with httpx.AsyncClient(headers=headers, timeout=8.0) as client:
                    self.on_connection_opened()
                    self.on_heartbeat()
                    retry_delay = 1.0  # 恢复重试延迟

                    while self.is_running:
                        # 轮询直播间公开弹幕与状态
                        events = await self._poll_events(client)
                        if events:
                            for ev in events:
                                self._dispatch_event(ev)
                        else:
                            self.on_heartbeat()

                        await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.on_connection_error(e, reason=f"快手直播间 {self.clean_room_id} 轮询信令异常")
                logger.warning(f"快手监听异常: {e}，将在 {retry_delay:.1f} 秒后重试...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)

        self.on_worker_stopped()

    async def _poll_events(self, client: Any) -> list:
        """调用快手公开 API 或网页端轮询接口获取最新消息批次"""
        api_url = "https://live.kuaishou.com/live_api/liveroom/danmaku"
        params = {
            "liveStreamId": self.clean_room_id,
            "cursor": self._cursor,
        }
        resp = await client.get(api_url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("result") == 1 and "data" in data:
                res_data = data["data"]
                self._cursor = str(res_data.get("cursor") or "")
                return res_data.get("messages", [])
            elif data.get("result") != 1:
                logger.debug(f"快手接口返回业务提示: {data.get('message', '未知')}")
        else:
            raise ConnectionError(f"快手接口响应异常 HTTP {resp.status_code}")
        return []

    def _dispatch_event(self, raw_msg: Dict[str, Any]):
        """结构化解析并投递弹幕事件"""
        msg_type = raw_msg.get("type") or raw_msg.get("msgType", "chat")
        user = raw_msg.get("userName") or raw_msg.get("senderName") or "快手老铁"
        content = raw_msg.get("content") or raw_msg.get("text") or ""

        # 1. 评论/普通发言
        if msg_type in ("chat", "comment"):
            # 促单关键词提升优先级
            is_priority = any(kw in content for kw in self.INTENT_KEYWORDS)
            priority = 1 if is_priority else 2
            payload = {
                "platform": "kuaishou",
                "text": content,
                "user_id": str(raw_msg.get("userId", "")),
            }
            self.on_event_received()
            self._invoke_callback("chat", user, payload, priority)

        # 2. 礼物打赏
        elif msg_type in ("gift", "WebcastGiftMessage"):
            gift_name = raw_msg.get("giftName") or "精美礼物"
            count = int(raw_msg.get("count", 1))
            coin = int(raw_msg.get("totalCoin") or raw_msg.get("coin", 0))
            priority = 0 if coin >= self.P0_GIFT_COIN_THRESHOLD else 1
            payload = {
                "platform": "kuaishou",
                "gift_name": gift_name,
                "count": count,
                "total_coin": coin,
                "user_id": str(raw_msg.get("userId", "")),
            }
            self.on_event_received()
            self._invoke_callback("gift", user, payload, priority)

        # 3. 点赞
        elif msg_type in ("like", "WebcastLikeMessage"):
            payload = {
                "platform": "kuaishou",
                "count": int(raw_msg.get("count", 1)),
            }
            self.on_event_received()
            self._invoke_callback("like", user, payload, 2)

        # 4. 进场
        elif msg_type in ("member", "join"):
            payload = {
                "platform": "kuaishou",
                "user_id": str(raw_msg.get("userId", "")),
            }
            self.on_event_received()
            self._invoke_callback("member", user, payload, 2)

    def _invoke_callback(self, event_type: str, user: str, payload: Dict[str, Any], priority: int):
        """安全触发业务回调"""
        try:
            res = self.on_event_callback(event_type, user, payload, priority)
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
        except Exception as e:
            logger.error(f"处理快手弹幕回调失败: {e}")
