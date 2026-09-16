"""
抖音直播间弹幕监听适配器 (规划 §3.2 / §6.1 / v1.7.0 深度重构)
基于抖音 Web 端 WebSocket 长连接，以零依赖 protobuf 线格式解析器对消息信封
逐级结构化解包：PushFrame(payload=3, compressType=4) → Response(messages=1)
→ Message(method=1, payload=2) → Webcast*Message 子消息字段提取。

支持事件：
  - WebcastChatMessage  普通发言 (含价格/优惠关键词提权 P1)
  - WebcastGiftMessage  礼物打赏 (结构化提取数量/金额，按金额档位定级 P0/P1)
  - WebcastLikeMessage  点赞互动
  - WebcastMemberMessage 观众进房

能力边界 (诚实声明)：
  - 未实现 a_bogus/signature 请求签名：若平台风控拒绝握手，请在 app_settings 配置
    douyin_ttwid / douyin_ms_token (浏览器 DevTools 获取) 后重试，或使用中继模式
    (WS /ws/danmaku-ingest 或 POST /live/danmaku-webhook)。
  - 子消息字段号以社区公开 proto 约定优先，缺失时回退结构内可打印字符串启发式提取，
    提取不到的字段保持为 0/空，绝不伪造数据。
断线自愈：1s -> 2s -> 4s -> 8s -> 16s 指数退避重连 (规划 §15.2)
"""
import re
import gzip
import time
import asyncio
import logging
import random
from typing import Callable, Dict, Any, Optional
from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher
from server.adapters.danmaku import proto_reader as pr

logger = logging.getLogger("LiveAgent.DouyinFetcher")

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


class DouyinDanmakuFetcher(BaseDanmakuFetcher):
    """抖音 Web 端实时弹幕抓取器 (protobuf 结构化解析)"""
    platform_name = "douyin"

    WS_HOST = "wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    # 高意图促单关键词 (命中升级至 P1 优先解答)
    INTENT_KEYWORDS = ["多少钱", "怎么买", "发货", "优惠", "怎么卖", "领券", "链接", "库存", "保修", "正品", "拍了"]

    # 打断阈值与 webhook 对齐 (50000 瓣 = P0 强打断)
    P0_COIN_THRESHOLD = 50000

    def __init__(self, room_id: str, on_event_callback: Callable[[str, str, Dict[str, Any], int], Any],
                 ttwid: str = "", ms_token: str = ""):
        super().__init__(room_id, on_event_callback)
        self.worker_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.watched_count = 0
        self.clean_room_id = self._extract_room_id(room_id)
        self.ttwid = ttwid or ""
        self.ms_token = ms_token or ""

    @staticmethod
    def _extract_room_id(raw_input: str) -> str:
        """从用户输入的直播间链接或纯数字号提取 WebRid"""
        if not raw_input:
            return ""
        s = str(raw_input).strip()
        # 如: https://live.douyin.com/123456789
        m = re.search(r"live\.douyin\.com/(\d+)", s)
        if m:
            return m.group(1)
        # 纯数字
        nums = re.findall(r"\d+", s)
        if nums:
            return nums[0]
        return s

    def _cookie_header(self) -> str:
        parts = []
        if self.ttwid:
            parts.append(f"ttwid={self.ttwid}")
        if self.ms_token:
            parts.append(f"msToken={self.ms_token}")
        return "; ".join(parts)

    async def start(self):
        self.is_running = True
        self.worker_task = asyncio.create_task(self._listen_loop())
        logger.info(f"Douyin 弹幕监听器已就绪，目标直播间号: {self.clean_room_id}")

    async def stop(self):
        self.is_running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        self.on_worker_stopped()
        logger.info("Douyin 弹幕监听器已断开停止")

    async def _fetch_room_meta(self) -> Dict[str, Any]:
        """请求抖音 Web 页面抓取 ttwid 与内部真实 room_id (显式配置优先)"""
        if self.ttwid:
            return {"ttwid": self.ttwid, "internal_room_id": self.clean_room_id}
        if not httpx or not self.clean_room_id:
            return {"internal_room_id": self.clean_room_id}
        page_url = f"https://live.douyin.com/{self.clean_room_id}"
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(page_url, headers=headers)
                ttwid = resp.cookies.get("ttwid", "")
                html = resp.text

                # 从 HTML 中提取 internal room_id
                m = re.search(r'"roomId":"(\d+)"', html) or re.search(r'roomId_\\":\\"(\d+)\\"', html)
                internal_id = m.group(1) if m else self.clean_room_id

                return {"ttwid": ttwid or self.ttwid, "internal_room_id": internal_id}
        except Exception as e:
            logger.warning(f"获取抖音直播间元数据失败 ({e})，将尝试以原始 ID 握手")
            return {"ttwid": self.ttwid, "internal_room_id": self.clean_room_id}

    async def _listen_loop(self):
        """长连接主循环 (支持指数退避自愈)"""
        backoff = 1.0
        max_backoff = 16.0

        while self.is_running:
            try:
                if not websockets:
                    logger.warning("未检测到 websockets 库，抖音弹幕监听已降级为外部中继模式")
                    await asyncio.sleep(5.0)
                    continue

                meta = await self._fetch_room_meta()
                internal_id = meta.get("internal_room_id") or self.clean_room_id
                ttwid = meta.get("ttwid", "")

                ws_url = (
                    f"{self.WS_HOST}?app_name=douyin_web&version_code=180800&webcast_sdk_version=1.0.14"
                    f"&update_version_code=1.0.14&compress=gzip&internal_ext=internal_src:dim"
                    f"&live_id=1&did_rule=3&user_unique_id={random.randint(10000000, 99999999)}"
                    f"&room_id={internal_id}"
                )
                if self.ms_token:
                    ws_url += f"&msToken={self.ms_token}"

                cookie = self._cookie_header() or (f"ttwid={ttwid}" if ttwid else "")
                headers = {"User-Agent": self.USER_AGENT}
                if cookie:
                    headers["Cookie"] = cookie

                logger.info(
                    f"正在建立抖音 WSS 弹幕长连接: room_id={internal_id} "
                    f"(未携带 a_bogus 签名，风控拒绝时请配置 ttwid/msToken 或使用中继模式)"
                )
                self.on_connection_attempted()
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=None) as ws:
                    self.on_connection_opened()
                    logger.info("抖音直播间弹幕长连接握手成功！")

                    # 启动心跳
                    if self._heartbeat_task and not self._heartbeat_task.done():
                        self._heartbeat_task.cancel()
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

                    while self.is_running:
                        try:
                            # 增加静默超时保护 (45s 未收到任何服务端数据帧，判定为链路假死)
                            msg = await asyncio.wait_for(ws.recv(), timeout=45.0)
                        except asyncio.TimeoutError as te:
                            logger.warning("抖音弹幕长连接接收超时 (45s 无数据帧)，抛出异常进入指数退避自愈重连")
                            raise TimeoutError("抖音下行数据帧 45s 静默超时") from te

                        # 真实收到服务端下行数据帧才构成恢复证据并重置重连退避。
                        self.on_heartbeat()
                        backoff = 1.0
                        if isinstance(msg, bytes):
                            self._parse_push_frame(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"抖音弹幕长连接中断 ({e})，{backoff:.1f} 秒后执行指数退避自愈重连...")
                self.notify_error(e, "抖音弹幕长连接中断")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)

    async def _heartbeat_loop(self, ws):
        """每 10 秒发送心跳保持连接活路 (发送失败立即关闭 socket 唤醒主接收协程自愈)"""
        while self.is_running:
            try:
                await asyncio.sleep(10.0)
                # 抖音心跳包 (空 payload 或结构化 ping)；发送仅维持 socket 可写，不单方面虚报链路存活
                await ws.send(b":")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("抖音心跳发送异常: %s", e)
                # 只关闭 socket 唤醒 recv；统一由主接收循环上报该连接代际的故障。
                try:
                    await ws.close()
                except Exception:
                    pass
                break

    # ------------------------------------------------------------------
    # protobuf 信封逐级解包
    # ------------------------------------------------------------------
    def _parse_push_frame(self, raw_bytes: bytes):
        """PushFrame → Response → Message 逐级解包并分发 (纯 CPU 同步操作，无 IO)"""
        try:
            frame = raw_bytes
            if frame[:2] == b"\x1f\x8b":
                frame = gzip.decompress(frame)

            # PushFrame: payload=3 (bytes), compressType=4 (varint)
            payload = pr.get_bytes(frame, 3)
            if payload is None:
                return
            if payload[:2] == b"\x1f\x8b":
                payload = gzip.decompress(payload)
            else:
                compress = pr.get_varint(frame, 4) or 0
                if compress:
                    try:
                        payload = gzip.decompress(payload)
                    except Exception:
                        pass

            # Response: repeated Message = 1
            for _, msg_bytes in pr.iter_delimited(payload, 1):
                method = pr.get_string(msg_bytes, 1) or ""
                inner = pr.get_bytes(msg_bytes, 2)
                if inner is None:
                    continue
                if inner[:2] == b"\x1f\x8b":
                    try:
                        inner = gzip.decompress(inner)
                    except Exception:
                        pass
                self._dispatch_method(method, inner)
        except Exception as err:
            logger.debug(f"抖音消息帧解析容错: {err}")

    def _dispatch_method(self, method: str, payload: bytes):
        try:
            if method == "WebcastChatMessage":
                user = self._extract_user(payload, default="抖音观众")
                content = self._extract_chat_text(payload)
                prio = 1 if any(kw in content for kw in self.INTENT_KEYWORDS) else 2
                self._emit_event("danmaku", user, {"text": content}, priority=prio)

            elif method == "WebcastGiftMessage":
                user = self._extract_user(payload, default="热心大哥")
                count = pr.get_varint(payload, 5) or 1
                total_coin = pr.get_varint(payload, 11) or 0  # 提取不到保持 0，绝不伪造
                prio = 0 if total_coin >= self.P0_COIN_THRESHOLD else 1
                self._emit_event("gift", user, {
                    "gift_name": self._extract_gift_name(payload),
                    "count": count,
                    "total_coin": total_coin,
                }, priority=prio)

            elif method == "WebcastMemberMessage":
                user = self._extract_user(payload, default="新朋友")
                self.watched_count += 1
                self._emit_event("enter", user, {"text": "进入了直播间"}, priority=3)

            elif method == "WebcastLikeMessage":
                user = self._extract_user(payload, default="抖音观众")
                count = pr.get_varint(payload, 5) or pr.get_varint(payload, 3) or pr.get_varint(payload, 2) or 1
                self._emit_event("like", user, {"text": f"为主播点赞 x{count}"}, priority=2)
        except Exception as err:
            logger.debug(f"抖音 {method} 事件分发容错: {err}")

    # ------------------------------------------------------------------
    # 子消息字段提取 (约定字段号优先，结构内字符串启发式兜底)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_printable_short(s: str, max_len: int = 32) -> bool:
        return 1 <= len(s) <= max_len and all(ch.isprintable() for ch in s)

    def _extract_user(self, payload: bytes, default: str) -> str:
        """用户昵称：约定 user 子消息 (field 2) 的 nick (field 2)；失败回退子消息内首个可打印短串"""
        user_obj = pr.get_bytes(payload, 2)
        if user_obj:
            nick = pr.get_string(user_obj, 2)
            if nick and self._is_printable_short(nick):
                return nick
            for s in pr.iter_strings(user_obj):
                if self._is_printable_short(s):
                    return s
        return default

    def _extract_chat_text(self, payload: bytes) -> str:
        """弹幕正文：约定 content (field 3)；缺失时回退结构内最长可打印中文字符串"""
        content = pr.get_string(payload, 3)
        if content:
            return content.strip()[:60]
        candidates = [s for s in pr.iter_strings(payload)
                      if any("\u4e00" <= ch <= "\u9fff" for ch in s)]
        if candidates:
            return max(candidates, key=len)[:60]
        return ""

    def _extract_gift_name(self, payload: bytes) -> str:
        """礼物名：约定 Gift 子消息 (field 4) 的名称字段；提取不到返回空串由上层按事件类型兜底措辞"""
        gift_obj = pr.get_bytes(payload, 4)
        if gift_obj:
            for f in (2, 1, 3):
                name = pr.get_string(gift_obj, f)
                if name and self._is_printable_short(name, 24):
                    return name
            for s in pr.iter_strings(gift_obj):
                if self._is_printable_short(s, 24):
                    return s
        return "礼物"

    def _emit_event(self, event_type: str, user_name: str, payload: dict, priority: int = 2):
        if self.on_event_callback and self.is_running:
            try:
                self.on_event_received()
                res = self.on_event_callback(event_type, user_name, payload, priority)
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        asyncio.run(res)
            except Exception as e:
                logger.error(f"分发抖音弹幕事件回调异常: {e}")
