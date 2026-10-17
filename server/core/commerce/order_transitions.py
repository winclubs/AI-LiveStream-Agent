# -*- coding: utf-8 -*-
"""
订单状态机单一事实来源 (server.core.commerce.order_transitions)

存在理由（上线诊断 BLOCKER-2）：
历史上订单状态推进存在**两份强度不同的实现**：

    server/routes/live_orders.py     原子条件 UPDATE (WHERE status='completed') + rowcount 校验
    server/routes/orders_webhook.py  Python 侧状态判断 (if status == "cancelled")

各分支只挡「自己重复自己」，跨状态不设防，于是::

    paid -> cancelled -> refunded
    库存 10 -> 7 -> 10 -> 13      # 二次回补，虚增库存

本模块把状态推进收敛为唯一实现，与 ``server.core.security.auth_policy``
同一思路：从结构上消除再次漂移的可能，而不是靠约定。

裁决规则（与 live_orders 历史行为逐条对齐，勿轻易调整）：

    1. 只有 ``completed``（已售）是唯一可推进的起始态；
    2. 推进以**条件 UPDATE 的 rowcount** 为唯一权威，不信任调用方读到的状态快照；
    3. rowcount == 1 -> APPLIED，恰好回补一次库存并写一条流水；
    4. 订单快照已是终态      -> SKIPPED（幂等，不回补）；
    5. 快照说可推进但 rowcount==0 -> CONFLICT（并发被别人抢先，回调侧按幂等成功处理）。

第 5 条是「读-改-写」竞态下唯一安全的裁决：条件 UPDATE 是数据库内的原子操作，
两个并发请求中必有一个 rowcount 为 0，从而保证**一次扣减至多一次回补**。
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal, get_args

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.models import InventoryMovement, Order, Product

logger = logging.getLogger("LiveAgent.OrderTransitions")

#: 唯一允许推进的起始态：已售出。
SOLD = "completed"
#: 终态：已取消 / 已退款。均已回补过库存，不可再次推进。
CANCELLED = "cancelled"
REFUNDED = "refunded"

Outcome = Literal["APPLIED", "SKIPPED", "CONFLICT"]
OUTCOMES = get_args(Outcome)

#: 终态集合：命中即代表库存早已回补过一次。
TERMINAL_STATUSES = frozenset({CANCELLED, REFUNDED})


async def _restock_once(db: AsyncSession, *, product_id: str, quantity: int) -> None:
    """回补库存。仅在 :data:`APPLIED` 路径调用，故全局至多执行一次/次扣减。"""
    await db.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(current_stock=Product.current_stock + quantity)
    )


async def _record_movement(
    db: AsyncSession, *, order_id: str, product_id: str, quantity_delta: int, reason: str
) -> None:
    db.add(
        InventoryMovement(
            id=f"mov_{uuid.uuid4().hex[:12]}",
            order_id=order_id,
            product_id=product_id,
            quantity_delta=quantity_delta,
            reason=reason,
        )
    )


async def _conditional_transition(
    db: AsyncSession,
    order: Order,
    *,
    values: dict,
    movement_reason: str,
) -> Outcome:
    """以条件 UPDATE 为唯一权威推进订单状态，并在成功时恰好回补一次库存。

    ``order`` 只需是已加载的 ORM 对象（提供 id / product_id / quantity / status 快照）。
    状态快照**仅用于区分幂等与并发**，不作为推进依据。

    实现约束：所有 ORM 属性在**任何 rollback 之前**读入局部变量。
    ``rollback()`` 会 expire 整个 session，此后访问 ``order.<attr>`` 会触发隐式
    同步 IO，在 async 栈上抛 MissingGreenlet。
    """
    order_id = order.id
    product_id = order.product_id
    quantity = order.quantity
    status_snapshot = order.status

    if status_snapshot in TERMINAL_STATUSES:
        # 已是终态：库存早已回补过，直接幂等返回，绝不二次回补。
        return "SKIPPED"

    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == SOLD)
        .values(**values)
        .execution_options(synchronize_session=False)
    )

    if getattr(result, "rowcount", 0) != 1:
        # 快照说可推进、数据库却拒绝 => 并发被别人抢先。按幂等处理，不回补。
        await db.rollback()
        logger.info("订单 %s 状态推进被并发抢先（目标 %s），按幂等跳过", order_id, sorted(values))
        return "CONFLICT"

    await _restock_once(db, product_id=product_id, quantity=quantity)
    await _record_movement(
        db,
        order_id=order_id,
        product_id=product_id,
        quantity_delta=quantity,
        reason=movement_reason,
    )
    return "APPLIED"


async def apply_cancel(db: AsyncSession, order: Order, *, movement_reason: str = "cancel") -> Outcome:
    """把订单从 ``completed`` 推进到 ``cancelled`` 并恰好回补一次库存。"""
    outcome = await _conditional_transition(
        db,
        order,
        values={"status": CANCELLED},
        movement_reason=movement_reason,
    )
    if outcome == "APPLIED":
        order.status = CANCELLED
    return outcome


async def apply_refund(
    db: AsyncSession,
    order: Order,
    *,
    refund_amount: float | None = None,
    movement_reason: str = "refund",
) -> Outcome:
    """把订单从 ``completed`` 推进到 ``refunded`` 并恰好回补一次库存。

    ``refund_amount`` 为 ``None`` 时表示整单退款，沿用订单实付金额。
    """
    amount = order.amount if refund_amount is None else refund_amount
    outcome = await _conditional_transition(
        db,
        order,
        values={"status": REFUNDED, "refund_amount": amount},
        movement_reason=movement_reason,
    )
    if outcome == "APPLIED":
        order.status = REFUNDED
        order.refund_amount = amount
    return outcome
