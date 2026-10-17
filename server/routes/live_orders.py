# -*- coding: utf-8 -*-
"""电商订单与场次统计：订单登记/查询/取消/退款、场次列表、实时统计。

由 server/routes/live.py 拆分而来。
`_refresh_order_stats` 是缝（被 server/routes/orders_webhook.py 导入），
故实现迁至本模块并改名 `refresh_order_stats`，live.py 保留同名薄缝转发。
本模块内的调用方一律经由该缝解析，以保住 patch 的晚绑定语义。
"""
import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.commerce.order_transitions import apply_cancel, apply_refund
from server.database.db import get_db
from server.database.models import AppSetting, Product

router = APIRouter()


class OrderRequest(BaseModel):
    amount: float = Field(gt=0, le=100_000_000)
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, gt=0, le=10_000)
    external_id: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default="", max_length=2000)


class RefundRequest(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0, le=100_000_000)


def _order_payload(order) -> dict:
    return {
        "id": order.id,
        "external_id": order.external_id,
        "session_id": order.session_id,
        "product_id": order.product_id,
        "sku": order.sku,
        "amount": order.amount,
        "quantity": order.quantity,
        "status": order.status,
        "refund_amount": order.refund_amount,
        "note": order.note or "",
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


async def refresh_order_stats(db: AsyncSession, session_id: Any) -> tuple[float, int]:

    from server.routes.live import global_live_controller
    from server.database.models import Order
    sid = str(session_id or "")
    result = await db.execute(
        select(func.coalesce(func.sum(Order.amount), 0.0), func.count(Order.id)).where(
            Order.session_id == sid, Order.status == "completed"
        )
    )
    gmv, count = result.one()
    aggregate = (round(float(gmv or 0.0), 2), int(count or 0))
    if sid == global_live_controller.session_id:
        global_live_controller.stats["gmv_yuan"] = aggregate[0]
        global_live_controller.stats["orders_count"] = aggregate[1]
    return aggregate


@router.post("/stats/order")
async def register_order(req: OrderRequest, db: AsyncSession = Depends(get_db)):
    """幂等登记成交，库存条件更新与订单/流水在同一事务提交。"""

    from server.routes.live import global_live_controller, _refresh_order_stats
    from server.database.models import InventoryMovement, Order

    if not global_live_controller.is_live or not global_live_controller.session_id:
        raise HTTPException(status_code=400, detail="直播间尚未开播")

    if global_live_controller.execution_mode == "DEMO":
        raise HTTPException(status_code=403, detail="【商业准入安全拦截】DEMO 演示模式严禁登记真实交易订单与扣减库存")

    external_id = (req.external_id or f"manual-{uuid.uuid4().hex}").strip()
    existing = (await db.execute(select(Order).where(Order.external_id == external_id))).scalar_one_or_none()
    if existing:
        gmv_yuan, orders_count = await _refresh_order_stats(db, str(existing.session_id))
        product = await db.get(Product, existing.product_id) if existing.product_id else None
        return {
            "code": 0,
            "message": "重复请求已按幂等订单返回",
            "data": {
                "order_id": existing.id,
                "gmv_yuan": gmv_yuan,
                "orders_count": orders_count,
                "remaining_stock": product.current_stock if product else None,
                "idempotent_replay": True,
            },
        }

    sku = req.sku.strip()
    product = (await db.execute(select(Product).where(Product.sku_code == sku, Product.is_active == 1))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"商品 SKU 不存在或已下架: {sku}")

    result = await db.execute(
        update(Product)
        .where(Product.id == product.id, Product.current_stock >= req.quantity)
        .values(current_stock=Product.current_stock - req.quantity)
    )
    if getattr(result, "rowcount", 0) != 1:
        await db.rollback()
        raise HTTPException(status_code=409, detail="商品库存不足")

    snapshot = {
        "id": product.id,
        "sku": product.sku_code,
        "title": product.title,
        "unit_live_price": product.live_price,
    }
    order = Order(
        id=f"ord_{uuid.uuid4().hex[:12]}",
        external_id=external_id,
        session_id=global_live_controller.session_id,
        product_id=product.id,
        sku=sku,
        amount=req.amount,
        quantity=req.quantity,
        status="completed",
        note=req.note or "",
        product_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    )
    db.add(order)
    await db.flush()
    db.add(InventoryMovement(
        id=f"mov_{uuid.uuid4().hex[:12]}", order_id=order.id,
        product_id=product.id, quantity_delta=-req.quantity, reason="sale"
    ))
    await db.commit()
    await db.refresh(product)
    gmv_yuan, orders_count = await _refresh_order_stats(db, str(order.session_id))
    return {
        "code": 0,
        "message": f"成交已登记并持久化：+{req.amount} 元",
        "data": {
            "order_id": order.id,
            "gmv_yuan": gmv_yuan,
            "orders_count": orders_count,
            "remaining_stock": product.current_stock,
            "idempotent_replay": False,
        },
    }


@router.get("/orders")
async def list_orders(session_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    from server.database.models import Order
    query = select(Order).order_by(Order.created_at.desc())
    if session_id:
        query = query.where(Order.session_id == session_id)
    rows = (await db.execute(query.limit(500))).scalars().all()
    return {"code": 0, "total": len(rows), "data": [_order_payload(row) for row in rows]}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, db: AsyncSession = Depends(get_db)):

    from server.routes.live import _refresh_order_stats
    from server.database.models import Order
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status != "completed":
        return {"code": 0, "message": "订单已处理", "data": _order_payload(order)}

    # 状态推进收敛到单一事实来源（上线诊断 BLOCKER-2）：与平台 Webhook 回调
    # 共用同一状态机，杜绝「回调路径回补两次、REST 路径回补一次」的强度差。
    outcome = await apply_cancel(db, order, movement_reason="cancel")
    if outcome == "CONFLICT":
        raise HTTPException(status_code=409, detail="订单状态已变化，请刷新后重试")
    await db.commit()
    await db.refresh(order)
    await _refresh_order_stats(db, str(order.session_id))
    return {"code": 0, "message": "订单已取消并回补库存", "data": _order_payload(order)}


@router.post("/orders/{order_id}/refund")
async def refund_order(order_id: str, req: RefundRequest, db: AsyncSession = Depends(get_db)):

    from server.routes.live import _refresh_order_stats
    from server.database.models import Order
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status != "completed":
        raise HTTPException(status_code=409, detail="当前订单状态不可退款")
    refund_amount = req.amount if req.amount is not None else order.amount
    if abs(refund_amount - order.amount) > 0.005:
        raise HTTPException(status_code=400, detail="当前版本仅支持整单退款")

    outcome = await apply_refund(db, order, refund_amount=refund_amount, movement_reason="refund")
    if outcome == "CONFLICT":
        raise HTTPException(status_code=409, detail="订单状态已变化，请刷新后重试")
    await db.commit()
    await db.refresh(order)
    await _refresh_order_stats(db, str(order.session_id))
    return {"code": 0, "message": "订单已退款并回补库存", "data": _order_payload(order)}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    from server.database.models import LiveSessionRecord
    rows = (await db.execute(select(LiveSessionRecord).order_by(LiveSessionRecord.start_time.desc()).limit(200))).scalars().all()
    data = [{
        "session_id": row.session_id,
        "platform": row.platform,
        "theme": row.theme,
        "mode": row.mode,
        "role_id": row.role_id,
        "voice_id": row.voice_id,
        "anchor_id": row.anchor_id,
        "status": row.status,
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "end_time": row.end_time.isoformat() if row.end_time else None,
        "total_gmv": row.total_gmv,
        "orders_count": row.orders_count,
        "danmaku_count": row.danmaku_count,
        "peak_viewers": row.peak_viewers,
        "gift_income": row.gift_income,
    } for row in rows]
    return {"code": 0, "total": len(data), "data": data}


@router.get("/stats")
async def get_live_stats(db: AsyncSession = Depends(get_db)):
    """
    直播大屏运营参数 (需求 8)：
    直播类型(模式)/主播是谁/场观/打赏/成交额/弹幕量/违规拦截/网络状态/直播时长
    """

    from server.routes.live import global_live_controller, global_role_manager
    from server.database.models import Anchor
    from server.routes.settings import SETTING_KEY_LIVE_MODE, SETTING_KEY_ANCHOR_ID

    stats = global_live_controller.stats
    is_live = global_live_controller.is_live

    anchor_id = None
    res = await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY_LIVE_MODE))
    row = res.scalar_one_or_none()
    raw_mode: Optional[str] = str(row.value) if (row and row.value is not None) else None

    from server.routes.settings import normalize_live_mode, LIVE_MODES
    norm_mode = normalize_live_mode(raw_mode)
    mode_name = LIVE_MODES.get(norm_mode, {}).get("name", "本地云端混合")

    res2 = await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY_ANCHOR_ID))
    row2 = res2.scalar_one_or_none()
    anchor_id: Optional[str] = str(row2.value) if (row2 and row2.value is not None) else None

    anchor_name: Optional[str] = None
    if anchor_id:
        res3 = await db.execute(select(Anchor).where(Anchor.id == anchor_id))
        anchor = res3.scalar_one_or_none()
        if anchor and anchor.name is not None:
            anchor_name = str(anchor.name)

    active_role = global_role_manager.get_active_role()
    duration_sec = int(time.time() - stats["start_ts"]) if (is_live and stats["start_ts"]) else 0

    return {
        "code": 0,
        "data": {
            "is_live": is_live,
            "session_id": global_live_controller.session_id,
            "mode": mode_name,
            "mode_code": norm_mode,
            "platform": global_live_controller.platform if is_live else None,
            "anchor_name": anchor_name or active_role.role_name,
            "role_name": active_role.role_name,
            "role_type": active_role.role_type,
            "duration_sec": duration_sec,
            "viewer_count": stats["viewer_count"] if is_live else 0,
            "peak_viewer_count": stats["peak_viewer_count"] if is_live else 0,
            "gift_income_yuan": stats["gift_income_yuan"] if is_live else 0.0,
            # 兼容字段：均表示被调度队列 accepted 的真实事件。
            "gift_count": stats["gift_count"] if is_live else 0,
            "gmv_yuan": stats["gmv_yuan"] if is_live else 0.0,
            "orders_count": stats["orders_count"] if is_live else 0,
            "danmaku_count": stats["danmaku_count"] if is_live else 0,
            "events_received_total": stats["events_received_total"] if is_live else 0,
            "events_accepted_total": stats["events_accepted_total"] if is_live else 0,
            "events_dropped_total": stats["events_dropped_total"] if is_live else 0,
            "danmaku_received_total": stats["danmaku_received_total"] if is_live else 0,
            "danmaku_accepted_total": stats["danmaku_accepted_total"] if is_live else 0,
            "danmaku_dropped_total": stats["danmaku_dropped_total"] if is_live else 0,
            "gift_received_total": stats["gift_received_total"] if is_live else 0,
            "gift_accepted_total": stats["gift_accepted_total"] if is_live else 0,
            "gift_dropped_total": stats["gift_dropped_total"] if is_live else 0,
            "queue_metrics": global_live_controller.event_queue.get_stats(),
            "guardrail_hits": stats["guardrail_hits"] if is_live else 0,
            "flash_sales": stats["flash_sales"] if is_live else 0,
            "stitcher_state": (
                global_live_controller.stitcher.state.value if (is_live and getattr(global_live_controller, "stitcher", None)) else "idle"
            ),
            "stitcher_badge": (
                (
                    "【静轨·高精切片】播发中" if global_live_controller.stitcher.state.value == "playing_batch_slice"
                    else "【打断中·平滑停顿】4-step 缓动停顿" if global_live_controller.stitcher.state.value == "interrupted_holding"
                    else "【动轨·实时驱动】正在互动答复观众" if global_live_controller.stitcher.state.value == "streaming_realtime"
                    else "【过渡中·断点回切】4-step 句首回退重入" if global_live_controller.stitcher.state.value == "blending_transition"
                    else "【动静融合中】"
                ) if (is_live and getattr(global_live_controller, "stitcher", None)) else "【待命中】"
            ),
            "active_slice_info": (
                {
                    "slice_id": global_live_controller.stitcher.current_slice.slice_id,
                    "product_id": global_live_controller.stitcher.current_slice.product_id,
                    "slot_code": global_live_controller.stitcher.current_slice.slot_code,
                    "duration_sec": global_live_controller.stitcher.current_slice.duration_sec,
                    "current_frame_idx": global_live_controller.stitcher.current_frame_idx,
                    "loudness_target": f"{global_live_controller.stitcher.current_slice.loudness_lufs:.1f} LUFS",
                    "architecture": "batch_stream_hybrid",
                    "diffusion_model": "ByteDance LatentSync UNet3D",
                } if (is_live and getattr(global_live_controller, "stitcher", None) and global_live_controller.stitcher.current_slice)
                else ({
                    "architecture": "batch_stream_hybrid",
                    "diffusion_model": "ByteDance LatentSync UNet3D",
                    "loudness_target": "-16.0 LUFS",
                    "status": "standby_or_idle",
                } if is_live else None)
            ),
            "network": {
                "ws_clients": len(ws_clients()),
                "status": "connected" if is_live else "idle"
            }
        }
    }


def ws_clients():
    """当前 WS 信令连接数 (网络状态指标)"""
    from server.routes.ws_live import ws_manager
    return ws_manager.active_connections
