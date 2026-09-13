import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set

router = APIRouter(tags=["WebSocket 实时推流监控"])

class ConnectionManager:
    """直播监控大屏全双工 WebSocket 连接管理器"""
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, event_name: str, payload: dict):
        """向所有打开大屏监控的客户端广播事件 (双向兼容协议字段)"""
        message = json.dumps({
            "event": event_name,
            "event_type": event_name,
            "payload": payload,
            "data": payload
        }, ensure_ascii=False)
        disconnected = []

        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.append(connection)
        for dead in disconnected:
            self.active_connections.discard(dead)

ws_manager = ConnectionManager()

@router.websocket("/ws/live_control")
async def websocket_live_control(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # 接收客户端控制指令（如人工插话、手动打断）
            data = await websocket.receive_text()
            msg = json.loads(data)
            event = msg.get("event")
            payload = msg.get("payload", {})

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
    报文: {"event_type":"danmaku|gift","user_name":"...","text":"...","gift_name":"...","total_coin":0}
    """
    import uuid as _uuid
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
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
                priority = 0 if total_coin >= 50000 else 1  # 与 /live/danmaku-webhook 的 P0 阈值对齐
                etype = "gift"
            else:
                payload = {"text": msg.get("text") or msg.get("message") or "", "platform": msg.get("platform", "relay")}
                priority = 2
                etype = "danmaku"
            await global_live_controller.event_queue.put(
                event_id=f"relay_{_uuid.uuid4().hex[:8]}",
                event_type=etype,
                user_name=user,
                payload=payload,
                priority=priority,
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
