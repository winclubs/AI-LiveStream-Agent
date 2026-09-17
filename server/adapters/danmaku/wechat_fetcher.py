# -*- coding: utf-8 -*-
"""
微信视频号直播弹幕监听适配器 (WechatDanmakuFetcher)
基于微信视频号助手 (Channels Live Assistant) 网页端协议与会话轮询，
支持提问促单提权、专业咨询高优先级响应以及高额打赏 P0 打断。
遵循 ADR-16 诚实契约与统一抓取器健康状态机。
"""

import re
import time
import asyncio
import logging
from typing import Callable, Dict, Any, Optional

from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher

logger = logging.getLogger("LiveAgent.WechatFetcher")

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


class WechatDanmakuFetcher(BaseDanmakuFetcher):
    """
    微信视频号直播实时弹幕抓取适配器
    支持事件：
      - chat: 观众提问/评论（结合法律/商品/专业咨询意图提权 P1）
      - gift: 微信豆/礼物打赏（大额礼物 >= 50000 豆升级 P0 强打断）
      - like: 喝彩点赞 (P2)
      - member: 进场互动 (P2)
    """
    platform_name = "wechat"

    INTENT_KEYWORDS = ["多少钱", "怎么买", "发货", "优惠", "咨询", "怎么联系", "链接", "库存", "正品", "请问", "老师"]

    P0_GIFT_THRESHOLD = 50000

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    def __init__(
        self,
        room_id: str,
        on_event_callback: Callable[[str, str, Dict[str, Any], int], Any],
        token: str = "",
        poll_interval: float = 2.0,
        **kwargs
    ):
        super().__init__(room_id, on_event_callback)
        self.clean_room_id = self._extract_room_id(room_id)
        self.token = token or ""
        self.poll_interval = max(1.0, float(poll_interval))
        self.worker_task: Optional[asyncio.Task] = None
        self._last_msg_id: str = ""
        self.watched_count: int = 0

    @staticmethod
    def _extract_room_id(raw_input: str) -> str:
        """从字符串中提取视频号 LiveId 或助学会话 ID"""
        if not raw_input:
            return ""
        s = str(raw_input).strip()
        # 匹配标准 exportId / liveId / 纯数字字母串
        m = re.search(r"liveId=([a-zA-Z0-9_\-]+)", s)
        if m:
            return m.group(1)
        matched = re.findall(r"[a-zA-Z0-9_\-]+", s)
        return matched[0] if matched else s

    async def start(self):
        """启动微信视频号监听工作协程"""
        self.is_running = True
        self.worker_task = asyncio.create_task(self._listen_loop())
        logger.info(f"微信视频号弹幕监听器已就绪，目标 LiveId: {self.clean_room_id or self.room_id}")

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
        logger.info(f"微信视频号 {self.clean_room_id} 弹幕监听已安全停止")

    async def _listen_loop(self):
        """核心监听与断线指数退避重试循环"""
        retry_delay = 1.0
        max_delay = 16.0

        headers = {
            "User-Agent": self.USER_AGENT,
            "Referer": "https://channels.weixin.qq.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if self.token:
            headers["Cookie"] = self.token if "=" in self.token else f"session_token={self.token}"

        while self.is_running:
            self.on_connection_attempted()
            try:
                if not httpx:
                    raise RuntimeError("未安装 httpx 依赖库，无法执行视频号网络请求")

                async with httpx.AsyncClient(headers=headers, timeout=8.0) as client:
                    self.on_connection_opened()
                    self.on_heartbeat()
                    retry_delay = 1.0

                    while self.is_running:
                        events = await self._poll_comments(client)
                        if events:
                            for ev in events:
                                self._dispatch_event(ev)
                        else:
                            self.on_heartbeat()

                        await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.on_connection_error(e, reason=f"视频号直播间 {self.clean_room_id} 轮询信令异常")
                logger.warning(f"视频号监听异常: {e}，将在 {retry_delay:.1f} 秒后重试...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)

        self.on_worker_stopped()

    async def _poll_comments(self, client: Any) -> list:
        """调用视频号助手后台 API 获取最新弹幕留言"""
        api_url = "https://channels.weixin.qq.com/cgi-bin/mmfinderliveassistant-bin/getlivecomment"
        payload = {
            "liveId": self.clean_room_id,
            "lastBuffer": self._last_msg_id,
        }
        resp = await client.post(api_url, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0 and "data" in data:
                res_data = data["data"]
                self._last_msg_id = str(res_data.get("lastBuffer") or "")
                return res_data.get("commentList", [])
            elif data.get("errcode") != 0:
                logger.debug(f"视频号接口返回业务提示: {data.get('errmsg', '未知')}")
        else:
            raise ConnectionError(f"微信视频号接口响应异常 HTTP {resp.status_code}")
        return []

    def _dispatch_event(self, raw_msg: Dict[str, Any]):
        """结构化解析并分发视频号弹幕事件"""
        msg_type = raw_msg.get("type") or raw_msg.get("msgType", "chat")
        user = raw_msg.get("nickname") or raw_msg.get("userName") or "微信观众"
        content = raw_msg.get("content") or raw_msg.get("description") or ""

        # 1. 评论/普通发言
        if msg_type in ("chat", "comment", 1):
            is_priority = any(kw in content for kw in self.INTENT_KEYWORDS)
            priority = 1 if is_priority else 2
            payload = {
                "platform": "wechat",
                "text": content,
                "user_id": str(raw_msg.get("fromUsername", "")),
            }
            self.on_event_received()
            self._invoke_callback("chat", user, payload, priority)

        # 2. 礼物打赏
        elif msg_type in ("gift", 2):
            gift_name = raw_msg.get("giftName") or "视频号好礼"
            count = int(raw_msg.get("count", 1))
            coin = int(raw_msg.get("totalCoin") or raw_msg.get("coin", 0))
            priority = 0 if coin >= self.P0_GIFT_THRESHOLD else 1
            payload = {
                "platform": "wechat",
                "gift_name": gift_name,
                "count": count,
                "total_coin": coin,
                "user_id": str(raw_msg.get("fromUsername", "")),
            }
            self.on_event_received()
            self._invoke_callback("gift", user, payload, priority)

        # 3. 点赞
        elif msg_type in ("like", 3):
            payload = {
                "platform": "wechat",
                "count": int(raw_msg.get("count", 1)),
            }
            self.on_event_received()
            self._invoke_callback("like", user, payload, 2)

        # 4. 进场
        elif msg_type in ("member", 4):
            payload = {
                "platform": "wechat",
                "user_id": str(raw_msg.get("fromUsername", "")),
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
            logger.error(f"处理视频号弹幕回调失败: {e}")
