import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set, Dict

logger = logging.getLogger("LiveAgent.WebSocketManager")
router = APIRouter(tags=["WebSocket 实时推流监控"])

class ConnectionManager:
    """直播监控大屏全双工 WebSocket 连接管理器 (带慢客户端隔离与有界背压)"""
    def __init__(self, client_queue_size: int = 100, send_timeout: float = 1.0):
        self.active_connections: Set[WebSocket] = set()
        self.client_queues: Dict[WebSocket, asyncio.Queue] = {}
        self.sender_tasks: Dict[WebSocket, asyncio.Task] = {}
        self.client_queue_size = client_queue_size
        self.send_timeout = send_timeout

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.client_queue_size)
        self.client_queues[websocket] = queue
        self.sender_tasks[websocket] = asyncio.create_task(self._sender_loop(websocket, queue))

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        self.client_queues.pop(websocket, None)
        task = self.sender_tasks.pop(websocket, None)
        if task and not task.done():
            task.cancel()

    async def _sender_loop(self, websocket: WebSocket, queue: asyncio.Queue):
        """每个客户端独立的有界发送循环，单次发送超时保护"""
        try:
            while True:
                message = await queue.get()
                try:
                    await asyncio.wait_for(websocket.send_text(message), timeout=self.send_timeout)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning("WebSocket 客户端发送超时或异常，主动断开慢客户端: %s", e)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.disconnect(websocket)
            try:
                await websocket.close()
            except Exception:
                pass

    async def broadcast(self, event_name: str, payload: dict):
        """非阻塞广播：消息写入各客户端独立有界队列，队列超限自动剔除慢客户端，绝不阻塞直播循环"""
        message = json.dumps({
            "event": event_name,
            "event_type": event_name,
            "payload": payload,
            "data": payload
        }, ensure_ascii=False)

        for ws, queue in list(self.client_queues.items()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("客户端发送队列已满，主动淘汰超载慢客户端")
                self.disconnect(ws)
                try:
                    asyncio.create_task(ws.close())
                except Exception:
                    pass

ws_manager = ConnectionManager()

@router.websocket("/ws/live_control")
async def websocket_live_control(websocket: WebSocket):
    from server.core.security.ws_auth import verify_websocket_auth
    if not await verify_websocket_auth(websocket):
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            # 接收客户端控制指令（如人工插话、手动打断）
            data = await websocket.receive_text()
            msg = json.loads(data)
            event = msg.get("event")

            if event == "PING":
                await websocket.send_text(json.dumps({"event": "PONG"}))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


@router.websocket("/ws/danmaku-ingest")
async def websocket_danmaku_ingest(websocket: WebSocket):
    """
    通用弹幕 WebSocket 接入端点：供本地弹幕中继器/浏览器脚本/第三方弹幕姬推送事件。
    统一进行来源鉴权、滑动窗口频控与 global_live_controller.ingest_event 归一化处理。
    """
    from server.core.security.ws_auth import verify_websocket_auth, WSRateLimiter
    if not await verify_websocket_auth(websocket):
        return

    await websocket.accept()
    rate_limiter = WSRateLimiter(max_requests=50, window_seconds=1.0)
    try:
        while True:
            raw = await websocket.receive_text()
            if not rate_limiter.allow_request():
                logger.warning("弹幕中继 WebSocket 频率超限，丢弃高频消息")
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            from server.routes.live import global_live_controller
            if not global_live_controller.is_live:
                continue
            event_type = (msg.get("event_type") or msg.get("type") or "danmaku").lower()
            user = msg.get("user_name") or msg.get("user") or "外部观众"
            if event_type in ("gift", "send_gift"):
                total_coin = int(msg.get("total_coin", 0) or 0)
                payload = {
                    "gift_name": msg.get("gift_name", "礼物"),
                    "count": int(msg.get("gift_count", 1) or 1),
                    "total_coin": total_coin,
                    "platform": msg.get("platform", "relay"),
                }
                priority = 0 if total_coin >= 50000 else 1
                etype = "gift"
            else:
                payload = {"text": msg.get("text") or msg.get("message") or "", "platform": msg.get("platform", "relay")}
                priority = 2
                etype = "danmaku"

            await global_live_controller.ingest_event(
                event_type=etype,
                user_name=user,
                payload=payload,
                priority=priority,
                source="websocket_relay"
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
