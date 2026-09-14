import asyncio
import inspect
import json
import struct
import zlib
import logging
import random
from typing import Callable, Dict, Any, Optional, List
from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher

logger = logging.getLogger("LiveAgent.BilibiliFetcher")

try:
    import brotli
except ImportError:
    brotli = None

try:
    import websockets
except ImportError:
    websockets = None

try:
    import httpx
except ImportError:
    httpx = None


class BilibiliDanmakuFetcher(BaseDanmakuFetcher):
    """
    Bilibili 直播间弹幕监听适配器（真实 WebSocket 协议实现）
    协议链路：
      1. GET getDanmuInfo 获取弹幕服务器列表与认证 token
      2. WSS 连接 broadcastlv 节点，发送 OP=7 认证包 (protover=2 zlib)
      3. 每 30 秒发送 OP=2 心跳，OP=3 回包携带人气值
      4. OP=5 消息包按 protover 解压 (zlib) 并逐帧解析 JSON
    事件映射：
      - DANMU_MSG      普通弹幕 (含促单关键词自动提权 P1)
      - SEND_GIFT      礼物打赏 (>=1000 金瓜子升级 P0 强打断)
      - INTERACT_WORD  观众进场
      - WATCHED_CHANGE 在线人数变化 (更新 live_context)
    断线后执行 1s -> 2s -> 4s -> 8s -> 16s 指数退避自愈重连
    """
    platform_name = "bilibili"

    DANMU_INFO_API = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
    WS_CANDIDATE_HOSTS = [
        "wss://broadcastlv.chat.bilibili.com/sub",
        "wss://shark2-dc-6.chat.bilibili.com/sub",
        "wss://shark2-dc-4.chat.bilibili.com/sub",
    ]

    OP_HEARTBEAT_REPLY = 3
    OP_NORMAL = 5
    OP_AUTH_REPLY = 8

    MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
    MAX_EXPANDED_BYTES = 16 * 1024 * 1024
    MAX_TOTAL_EXPANDED_BYTES = 32 * 1024 * 1024
    MAX_PACKETS = 4096
    MAX_NESTING_DEPTH = 4

    def __init__(self, room_id: str, on_event_callback: Callable[[str, str, Dict[str, Any], int], Any]):
        super().__init__(room_id, on_event_callback)
        self.worker_task: Optional[asyncio.Task] = None
        self.watched_count = 0
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self):
        self.is_running = True
        self.worker_task = asyncio.create_task(self._listen_loop())
        logger.info(f"Bilibili 弹幕监听器已就绪，目标房间号: {self.room_id}")

    async def stop(self):
        self.is_running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        logger.info(f"Bilibili 房间 {self.room_id} 弹幕监听已断开")

    # ------------------------------------------------------------------
    # 真实协议层
    # ------------------------------------------------------------------
    async def _fetch_danmu_info(self) -> Dict[str, Any]:
        """调用 B 站开放接口获取弹幕服务器列表与认证 token"""
        if httpx is None:
            raise RuntimeError("httpx 未安装，无法建立 Bilibili 弹幕连接")
        url = f"{self.DANMU_INFO_API}?id={self.room_id}&type=0"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://live.bilibili.com/{self.room_id}",
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"getDanmuInfo 接口返回异常: {body.get('message', body.get('code'))}")
        token = body["data"]["token"]
        hosts = [f'wss://{h["host"]}:{h["wss_port"]}{h.get("wss_host", "") or "/sub"}' for h in body["data"]["host_list"]]
        return {"token": token, "hosts": hosts or self.WS_CANDIDATE_HOSTS}

    @staticmethod
    def _pack_packet(protover: int, operation: int, body: bytes) -> bytes:
        """构造 16 字节头 (total_len, header_len=16, protover, operation, sequence) + body"""
        total_len = 16 + len(body)
        return struct.pack(">IHHII", total_len, 16, protover, operation, 1) + body

    @classmethod
    def _unpack_packets(cls, data: bytes) -> List[tuple]:
        """严格切分协议帧，拒绝畸形头并限制单批帧数。"""
        packets = []
        offset = 0
        while offset + 16 <= len(data) and len(packets) < cls.MAX_PACKETS:
            (total_len, header_len, protover, operation, _seq) = struct.unpack(">IHHII", data[offset:offset + 16])
            if (
                total_len < 16
                or header_len < 16
                or header_len > total_len
                or offset + total_len > len(data)
            ):
                break
            body = data[offset + header_len: offset + total_len]
            packets.append((protover, operation, body))
            offset += total_len
        return packets

    @classmethod
    def _decompress_zlib_limited(cls, body: bytes) -> bytes:
        if len(body) > cls.MAX_COMPRESSED_BYTES:
            raise ValueError("zlib 压缩帧超过输入上限")
        decoder = zlib.decompressobj()
        payload = decoder.decompress(body, cls.MAX_EXPANDED_BYTES + 1)
        if len(payload) > cls.MAX_EXPANDED_BYTES or decoder.unconsumed_tail:
            raise ValueError("zlib 展开数据超过输出上限")
        tail = decoder.flush()
        if len(payload) + len(tail) > cls.MAX_EXPANDED_BYTES:
            raise ValueError("zlib 展开数据超过输出上限")
        return payload + tail

    @classmethod
    def _decompress_brotli_limited(cls, body: bytes) -> bytes:
        if len(body) > cls.MAX_COMPRESSED_BYTES:
            raise ValueError("Brotli 压缩帧超过输入上限")
        payload = brotli.decompress(body)
        if len(payload) > cls.MAX_EXPANDED_BYTES:
            raise ValueError("Brotli 展开数据超过输出上限")
        return payload

    async def _heartbeat(self, ws):
        """每 30 秒发送一次 OP=2 心跳保活"""
        try:
            while True:
                await ws.send(self._pack_packet(1, 2, b"[object Object]"))
                await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _listen_loop(self):
        """
        长连接监听主循环 (1s -> 2s -> 4s -> 8s -> 16s 指数退避自愈状态机)
        """
        if websockets is None:
            logger.error("未安装 websockets 库，Bilibili 真实弹幕连接不可用，请执行 pip install websockets")
            return

        retry_delay = 1.0
        max_delay = 16.0

        while self.is_running:
            try:
                info = await self._fetch_danmu_info()
                token = info["token"]
                hosts = info["hosts"]
                random.shuffle(hosts)
                connected = False

                for ws_url in hosts:
                    if not self.is_running:
                        return
                    try:
                        logger.info(f"正在建立 Bilibili 弹幕 WebSocket 握手: {ws_url} (房间 {self.room_id})")
                        async with websockets.connect(ws_url, max_size=2 ** 22) as ws:
                            auth_body = json.dumps({
                                "uid": 0,
                                "roomid": int(self.room_id),
                                "protover": 2,
                                "platform": "web",
                                "type": 2,
                                "key": token,
                            }, ensure_ascii=False).encode("utf-8")
                            await ws.send(self._pack_packet(1, 7, auth_body))

                            self._heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                            retry_delay = 1.0
                            connected = True
                            logger.info(f"Bilibili 房间 {self.room_id} 弹幕长连接握手完成，等待认证")

                            try:
                                async for raw in ws:
                                    if not self.is_running:
                                        return
                                    await self._handle_raw_packet(raw)
                            finally:
                                if self._heartbeat_task and not self._heartbeat_task.done():
                                    self._heartbeat_task.cancel()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"Bilibili 节点 {ws_url} 连接异常: {e}，尝试下一节点...")
                        continue

                if not connected and self.is_running:
                    raise RuntimeError("所有弹幕服务器节点均连接失败")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.is_running:
                    break
                logger.error(f"Bilibili 弹幕长连接断开: {e}，将在 {retry_delay} 秒后自动自愈重连...")
                self.notify_error(e, "Bilibili 弹幕长连接断开")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)

    async def _handle_raw_packet(self, raw: bytes, *, depth: int = 0, budget: Optional[dict] = None):
        """解析业务包，并限制递归深度、累计展开大小和帧数。"""
        if not isinstance(raw, (bytes, bytearray)):
            return
        if depth > self.MAX_NESTING_DEPTH:
            logger.warning("Bilibili 压缩协议嵌套超过上限，已丢弃")
            return
        if budget is None:
            budget = {"expanded_bytes": 0, "packets": 0}
        budget["expanded_bytes"] += len(raw)
        if budget["expanded_bytes"] > self.MAX_TOTAL_EXPANDED_BYTES:
            logger.warning("Bilibili 协议累计展开数据超过上限，已丢弃")
            return

        packets = self._unpack_packets(bytes(raw))
        budget["packets"] += len(packets)
        if budget["packets"] > self.MAX_PACKETS:
            logger.warning("Bilibili 单条消息协议帧数超过上限，已丢弃")
            return

        for protover, operation, body in packets:
            if operation == self.OP_AUTH_REPLY:
                self.on_connection_opened()
                logger.info(f"Bilibili 房间 {self.room_id} 认证通过 (AUTH_REPLY)")
            elif operation == self.OP_HEARTBEAT_REPLY:
                self.on_heartbeat()
                if len(body) >= 4:
                    popularity = struct.unpack(">I", body[:4])[0]
                    self.watched_count = popularity
            elif operation == self.OP_NORMAL:
                if protover in (0, 1):
                    await self._dispatch_json_message(body)
                    continue

                if protover == 2:
                    try:
                        payload_bytes = self._decompress_zlib_limited(body)
                    except (zlib.error, ValueError) as exc:
                        logger.warning("zlib 解压弹幕包失败: %s", exc)
                        continue
                elif protover == 3:
                    if brotli is None:
                        logger.error("收到 Brotli 弹幕帧但未安装 Brotli 运行时依赖")
                        continue
                    try:
                        payload_bytes = self._decompress_brotli_limited(body)
                    except Exception as exc:
                        logger.warning("Brotli 解压弹幕包失败: %s", exc)
                        continue
                else:
                    logger.debug("忽略未知 Bilibili 协议版本: %s", protover)
                    continue

                # 压缩体通常包含完整协议包；少数节点会直接返回 JSON。
                if payload_bytes.lstrip().startswith(b"{"):
                    budget["expanded_bytes"] += len(payload_bytes)
                    if budget["expanded_bytes"] > self.MAX_TOTAL_EXPANDED_BYTES:
                        logger.warning("Bilibili 协议累计展开数据超过上限，已丢弃")
                        return
                    await self._dispatch_json_message(payload_bytes)
                else:
                    await self._handle_raw_packet(payload_bytes, depth=depth + 1, budget=budget)

    async def _dispatch_json_message(self, body: bytes):
        try:
            msg = json.loads(body.decode("utf-8", errors="ignore"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        cmd = msg.get("cmd", "")
        if cmd.startswith("DANMU_MSG"):
            info = msg.get("info", [])
            try:
                text = info[1]
                user = info[2][1] if len(info) > 2 and len(info[2]) > 1 else "B站观众"
            except (IndexError, TypeError):
                return
            data = {"text": text}
            await self.inject_parsed_packet("DANMU_MSG", {"uname": user, "msg": text, "_data": data})
        elif cmd == "SEND_GIFT":
            data = msg.get("data", {})
            total_coin = data.get("total_coin") or (data.get("price", 0) * data.get("num", 1))
            await self.inject_parsed_packet("SEND_GIFT", {
                "uname": data.get("uname", "热心观众"),
                "giftName": data.get("giftName", "小红心"),
                "num": data.get("num", 1),
                "coin_type": data.get("coin_type", "gold"),
                "total_coin": total_coin,
            })
        elif cmd == "INTERACT_WORD":
            data = msg.get("data", {})
            await self.inject_parsed_packet("INTERACT_WORD", {"uname": data.get("uname", "新观众")})

    # ------------------------------------------------------------------
    # 协议包 -> 标准事件映射 (供真实链路与单测复用)
    # ------------------------------------------------------------------
    async def _emit_event(self, event_type: str, user: str, payload: dict, priority: int):
        """兼容同步和异步事件回调，避免对同步生产回调执行 await None。"""
        result = self.on_event_callback(event_type, user, payload, priority)
        if inspect.isawaitable(result):
            await result

    async def inject_parsed_packet(self, cmd: str, data: dict):
        """将 B 站原生协议消息映射为统一优先级事件。"""
        try:
            if cmd == "DANMU_MSG":
                user = data.get("uname", "B站观众")
                text = data.get("msg", "")
                priority = 2
                if any(kw in text for kw in ["怎么买", "多少钱", "有优惠吗", "发什么快递", "库存"]):
                    priority = 1
                await self._emit_event("danmaku", user, {"text": text}, priority)

            elif cmd == "SEND_GIFT":
                user = data.get("uname", "热心观众")
                gift_name = data.get("giftName", "小红心")
                total_coin = int(data.get("total_coin", 100) or 0)
                priority = 0 if total_coin >= 1000 else 1
                await self._emit_event("gift", user, {
                    "gift_name": gift_name,
                    "count": data.get("num", 1),
                    "total_coin": total_coin,
                }, priority)

            elif cmd == "INTERACT_WORD":
                user = data.get("uname", "新观众")
                await self._emit_event("entry", user, {}, 2)

        except Exception as exc:
            logger.error("处理 Bilibili 业务事件异常: %s", exc, exc_info=True)
