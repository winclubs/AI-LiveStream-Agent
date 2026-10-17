# -*- coding: utf-8 -*-
"""
电商订单平台 Webhook 闭环与对账服务 (依据《商业化运营改造方案》§7.1 与 §12)
支持签名校验、防重放、乱序保护、库存回补与对账差异审计。
"""
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.commerce.order_transitions import (
    TERMINAL_STATUSES,
    apply_cancel,
    apply_refund,
)
from server.database.db import get_db
from server.database.models import InventoryMovement, LiveSessionRecord, Order, Product
from server.routes.live import global_live_controller, _refresh_order_stats

logger = logging.getLogger("LiveAgent.OrderWebhook")
router = APIRouter(prefix="/orders", tags=["订单平台闭环"])

# 内存死信队列 (用于审计不可重放或校验失败的异常事件)
DEAD_LETTER_QUEUE: List[Dict[str, Any]] = []


def verify_webhook_signature(
    raw_body: bytes,
    signature: Optional[str],
    timestamp: Optional[str],
    secret: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    校验平台 Webhook 请求签名与时间戳 (防重放与防伪造)
    """
    configured_secret = secret or os.getenv("ORDER_WEBHOOK_SECRET", "").strip()
    if not configured_secret:
        # 生产/试点环境禁止无签名放行；仅显式 DEMO/TEST 才允许调用方跳过。
        execution_mode = os.getenv("LIVE_AGENT_MODE", "").strip().upper()
        allow_unsigned = os.getenv("LIVE_AGENT_ALLOW_UNSIGNED_WEBHOOK", "0").strip().lower() in ("1", "true", "yes")
        if execution_mode in ("PRODUCTION", "PILOT") or not allow_unsigned:
            return False, "MISSING_PRODUCTION_SECRET"
        logger.warning("订单 Webhook 未配置签名密钥，仅因显式开发开关放行")
        return True, "DEVELOPMENT_MODE"

    if not signature or not timestamp:
        return False, "MISSING_SIGNATURE_OR_TIMESTAMP"

    try:
        req_ts = float(timestamp)
        now = time.time()
        # 允许最大 300 秒时间差，防止重放攻击
        if abs(now - req_ts) > 300.0:
            return False, "TIMESTAMP_EXPIRED"
    except (ValueError, TypeError):
        return False, "INVALID_TIMESTAMP"

    # HMAC-SHA256: secret + timestamp + body
    message = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(
        configured_secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return False, "SIGNATURE_MISMATCH"

    return True, "VERIFIED"


class WebhookOrderEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(description="order.paid / order.shipped / order.cancelled / order.refunded")
    platform_order_id: str = Field(min_length=1, max_length=128)
    sku: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    session_id: Optional[str] = None
    timestamp: Optional[float] = None
    note: Optional[str] = None


@router.post("/webhook", summary="平台订单 Webhook 异步回调接口 (验签、幂等与状态机)")
async def handle_order_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
    x_timestamp: Optional[str] = Header(None, alias="X-Timestamp"),
):
    body_bytes = await request.body()
    is_valid, reason = verify_webhook_signature(body_bytes, x_signature, x_timestamp)
    if not is_valid:
        DEAD_LETTER_QUEUE.append({
            "id": f"dead_{uuid.uuid4().hex[:8]}",
            "reason": reason,
            "raw_body": body_bytes.decode("utf-8", errors="replace")[:1000],
            "timestamp": time.time(),
        })
        raise HTTPException(status_code=401, detail=f"Webhook 验签失败: {reason}")

    try:
        data = json.loads(body_bytes)
        event = WebhookOrderEvent(**data)
    except Exception as exc:
        DEAD_LETTER_QUEUE.append({
            "id": f"dead_{uuid.uuid4().hex[:8]}",
            "reason": f"PAYLOAD_PARSE_ERROR: {exc}",
            "raw_body": body_bytes.decode("utf-8", errors="replace")[:1000],
            "timestamp": time.time(),
        })
        raise HTTPException(status_code=400, detail=f"Webhook 数据结构不合规: {exc}")

    # 1. 查找是否存在已有关联订单 (以平台订单号作为幂等键)
    order_stmt = select(Order).where(Order.external_id == event.platform_order_id)
    existing_order = (await db.execute(order_stmt)).scalar_one_or_none()

    session_id = event.session_id or global_live_controller.session_id or "default_session"

    # Demo 会话严禁接收真实订单或改变库存；该校验独立于手工订单接口。
    session_record = await db.get(LiveSessionRecord, session_id) if session_id != "default_session" else None
    session_mode = str(getattr(session_record, "execution_mode", "") or "").upper()
    if session_mode == "DEMO" or global_live_controller.execution_mode == "DEMO":
        DEAD_LETTER_QUEUE.append({
            "id": f"dead_{uuid.uuid4().hex[:8]}",
            "reason": "DEMO_ORDER_BLOCKED",
            "event_id": event.event_id,
            "platform_order_id": event.platform_order_id,
            "timestamp": time.time(),
        })
        raise HTTPException(status_code=403, detail="DEMO 演示会话禁止登记真实订单")

    if event.event_type in ("order.created", "order.paid"):
        if existing_order:
            # 幂等返回：重复事件不重复扣减库存
            return {
                "code": 0,
                "message": "订单事件已幂等消费 (重复推送)",
                "data": {"order_id": existing_order.id, "status": existing_order.status},
            }

        # 校验商品与库存
        prod_stmt = select(Product).where(Product.sku_code == event.sku, Product.is_active == 1)
        product = (await db.execute(prod_stmt)).scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"商品 SKU 不存在或已下架: {event.sku}")

        # 原子扣减库存
        stock_res = await db.execute(
            update(Product)
            .where(Product.id == product.id, Product.current_stock >= event.quantity)
            .values(current_stock=Product.current_stock - event.quantity)
        )
        if getattr(stock_res, "rowcount", 0) != 1:
            await db.rollback()
            raise HTTPException(status_code=409, detail="库存不足，无法完成履约扣减")

        new_order = Order(
            id=f"ord_{uuid.uuid4().hex[:12]}",
            external_id=event.platform_order_id,
            session_id=session_id,
            product_id=product.id,
            sku=event.sku,
            amount=event.amount,
            quantity=event.quantity,
            status="completed" if event.event_type == "order.paid" else "pending",
            note=f"Webhook: {event.note or ''}",
        )
        db.add(new_order)
        await db.flush()
        db.add(InventoryMovement(
            id=f"mov_{uuid.uuid4().hex[:12]}",
            order_id=new_order.id,
            product_id=product.id,
            quantity_delta=-event.quantity,
            reason="webhook_sale",
        ))
        await db.commit()
        await _refresh_order_stats(db, session_id)
        return {
            "code": 0,
            "message": "平台订单已成功登记入库",
            "data": {"order_id": new_order.id, "status": new_order.status},
        }

    elif event.event_type == "order.cancelled":
        if not existing_order:
            raise HTTPException(status_code=404, detail="未找到待取消的本地订单")
        if existing_order.status in TERMINAL_STATUSES:
            return {"code": 0, "message": "订单已处于终态 (幂等处理)"}

        # 状态推进收敛到单一事实来源：以条件 UPDATE 的 rowcount 为唯一权威，
        # 保证「一次扣减至多回补一次库存」（上线诊断 BLOCKER-2）。
        outcome = await apply_cancel(
            db, existing_order, movement_reason="webhook_cancel_restore"
        )
        await db.commit()
        if outcome != "APPLIED":
            return {"code": 0, "message": f"订单取消未生效 ({outcome})，已按幂等跳过"}
        await _refresh_order_stats(db, str(existing_order.session_id))
        return {"code": 0, "message": "订单已取消，库存已自动回补"}

    elif event.event_type == "order.refunded":
        if not existing_order:
            raise HTTPException(status_code=404, detail="未找到待退款的本地订单")
        if existing_order.status in TERMINAL_STATUSES:
            return {"code": 0, "message": "订单已处于终态 (幂等处理)"}

        # 同上：复用唯一状态机，跨状态（如 cancelled -> refunded）不再二次回补库存。
        outcome = await apply_refund(
            db,
            existing_order,
            refund_amount=event.amount,
            movement_reason="webhook_refund_restore",
        )
        await db.commit()
        if outcome != "APPLIED":
            return {"code": 0, "message": f"订单退款未生效 ({outcome})，已按幂等跳过"}
        await _refresh_order_stats(db, str(existing_order.session_id))
        return {"code": 0, "message": "订单退款已完成，库存已回补"}

    else:
        raise HTTPException(status_code=400, detail=f"不支持的订单事件类型: {event.event_type}")


class RemoteOrderItem(BaseModel):
    platform_order_id: str
    amount: float
    status: str  # paid / cancelled / refunded


class ReconcileRequest(BaseModel):
    session_id: Optional[str] = None
    platform_orders: List[RemoteOrderItem]


@router.post("/reconcile", summary="平台订单对账与差异报告接口")
async def reconcile_orders(req: ReconcileRequest, db: AsyncSession = Depends(get_db)):
    """核对本地订单与平台官方对账单，输出差异明细"""
    query = select(Order)
    if req.session_id:
        query = query.where(Order.session_id == req.session_id)
    local_orders = (await db.execute(query)).scalars().all()

    local_map = {str(o.external_id): o for o in local_orders if o.external_id}

    matched_count = 0
    mismatches: List[Dict[str, Any]] = []
    missing_in_local: List[str] = []

    for r_item in req.platform_orders:
        p_id = r_item.platform_order_id
        if p_id not in local_map:
            missing_in_local.append(p_id)
            continue

        local_order = local_map[p_id]
        amount_diff = abs(local_order.amount - r_item.amount) > 0.01

        # 状态对齐映射
        status_matched = True
        if r_item.status == "paid" and local_order.status != "completed":
            status_matched = False
        elif r_item.status == "refunded" and local_order.status != "refunded":
            status_matched = False
        elif r_item.status == "cancelled" and local_order.status != "cancelled":
            status_matched = False

        if amount_diff or not status_matched:
            mismatches.append({
                "platform_order_id": p_id,
                "local_order_id": local_order.id,
                "local_amount": local_order.amount,
                "platform_amount": r_item.amount,
                "local_status": local_order.status,
                "platform_status": r_item.status,
            })
        else:
            matched_count += 1

    return {
        "code": 0,
        "summary": {
            "total_local": len(local_orders),
            "total_platform": len(req.platform_orders),
            "matched_count": matched_count,
            "mismatch_count": len(mismatches),
            "missing_in_local_count": len(missing_in_local),
        },
        "mismatches": mismatches,
        "missing_in_local": missing_in_local,
    }


@router.get("/dead-letters", summary="查看订单 Webhook 死信审计队列")
async def get_dead_letters():
    return {"code": 0, "total": len(DEAD_LETTER_QUEUE), "data": DEAD_LETTER_QUEUE[-50:]}
