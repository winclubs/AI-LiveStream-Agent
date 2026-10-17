# -*- coding: utf-8 -*-
"""订单状态机单一事实来源守护 (B2)

背景（上线诊断 BLOCKER-2）::

    paid -> cancelled -> refunded   库存 start=10 -> 7 -> 10 -> 13

订单 Webhook 曾用 Python 侧状态判断 (``if status == "cancelled"``) 推进状态，
而 REST 路由用原子条件 UPDATE (``WHERE status='completed'`` + rowcount)。
两套强度不同的实现并行演进，导致「已取消」订单再收退款回调时**二次回补库存**。

本文件以 HTTP 端点为唯一观察面（不打桩、不 mock 业务逻辑），钉死两条不变量：

1. **库存至多回补一次** —— 任意状态序列下，一次扣减只能对应一次回补。
2. **REST 与 Webhook 对同一状态给出一致裁决** —— 收敛到同一状态机。
"""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from server.app import app
from server.database.db import get_db
from server.database.models import Base, InventoryMovement, LiveSessionRecord, Order, Product

SECRET = "order_state_machine_secret"
TOKEN = "order_state_machine_token"


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """独立 DB + 商业档位，隔离订单状态机行为。"""
    monkeypatch.setenv("LIVE_AGENT_MODE", "PILOT")
    monkeypatch.setenv("API_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ORDER_WEBHOOK_SECRET", SECRET)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'osm.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _cfg(c, _r):
        cur = c.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await conn.run_sync(Base.metadata.create_all)

    async def get_db_override():
        async with factory() as session:
            yield session

    asyncio.run(setup())
    app.dependency_overrides[get_db] = get_db_override
    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def _hook(client, event_type, platform_id, sku, session_id, quantity=3, amount=50.0):
    """发送一条**签名合法**的平台回调。"""
    payload = {
        "event_id": f"ev_{uuid.uuid4().hex[:8]}",
        "event_type": event_type,
        "platform_order_id": platform_id,
        "sku": sku,
        "amount": amount,
        "quantity": quantity,
        "session_id": session_id,
    }
    body = json.dumps(payload).encode("utf-8")
    ts = str(time.time())
    sig = hmac.new(SECRET.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/orders/webhook",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": ts,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
    )


def _stock(factory, sku):
    async def q():
        async with factory() as s:
            row = (await s.execute(select(Product).where(Product.sku_code == sku))).scalar_one()
            return row.current_stock
    return asyncio.run(q())


def _order_id(factory, platform_id):
    async def q():
        async with factory() as s:
            row = (await s.execute(select(Order).where(Order.external_id == platform_id))).scalar_one()
            return row.id
    return asyncio.run(q())


def _restock_movements(factory, platform_id):
    async def q():
        async with factory() as s:
            order = (await s.execute(select(Order).where(Order.external_id == platform_id))).scalar_one()
            rows = (
                await s.execute(
                    select(InventoryMovement).where(InventoryMovement.order_id == order.id)
                )
            ).scalars().all()
            return order.status, [(r.reason, r.quantity_delta) for r in rows]
    return asyncio.run(q())


def _seed(factory, session_id, sku, stock=10):
    async def q():
        async with factory() as s:
            s.add(LiveSessionRecord(session_id=session_id, platform="mock"))
            s.add(Product(id=f"pr_{uuid.uuid4().hex[:10]}", sku_code=sku,
                          title="状态机测试商品", current_stock=stock, is_active=1))
            await s.commit()
    asyncio.run(q())


def test_webhook_refund_after_cancel_does_not_double_restock(iso):
    """paid -> cancelled -> refunded 的库存终态必须等于起点。

    修复前：start=10 -> paid 7 -> cancelled 10 -> refunded 13（虚增 3）。
    """
    client, factory = iso
    sku = f"OSM-{uuid.uuid4().hex[:8]}"
    session_id = f"sess_osm_{uuid.uuid4().hex[:8]}"
    pid = f"p_{uuid.uuid4().hex[:10]}"
    _seed(factory, session_id, sku, stock=10)
    start = _stock(factory, sku)

    assert _hook(client, "order.paid", pid, sku, session_id).status_code == 200
    assert _stock(factory, sku) == start - 3

    assert _hook(client, "order.cancelled", pid, sku, session_id).status_code == 200
    assert _stock(factory, sku) == start, "取消应恰好回补一次"

    assert _hook(client, "order.refunded", pid, sku, session_id).status_code == 200
    final = _stock(factory, sku)

    status, movements = _restock_movements(factory, pid)
    assert final == start, (
        f"库存虚增：起点 {start}，paid->cancelled->refunded 后为 {final}，"
        f"多出 {final - start} 件（订单状态 {status}，流水 {movements}）"
    )
    # 一次扣减只允许对应一条回补流水。
    restocks = [m for m in movements if m[1] > 0]
    assert len(restocks) == 1, f"回补流水应恰好 1 条，实际 {movements}"


def test_webhook_repeated_cancel_is_idempotent(iso):
    """重复取消不得二次回补库存。"""
    client, factory = iso
    sku, session_id = f"OSM-{uuid.uuid4().hex[:8]}", f"sess_osm_{uuid.uuid4().hex[:8]}"
    pid = f"p_{uuid.uuid4().hex[:10]}"
    _seed(factory, session_id, sku, stock=8)
    start = _stock(factory, sku)

    assert _hook(client, "order.paid", pid, sku, session_id).status_code == 200
    for _ in range(3):
        assert _hook(client, "order.cancelled", pid, sku, session_id).status_code == 200

    final = _stock(factory, sku)
    _, movements = _restock_movements(factory, pid)
    assert final == start, f"重复取消导致库存虚增：{start} -> {final}（流水 {movements}）"


def test_webhook_repeated_refund_is_idempotent(iso):
    """重复退款不得二次回补库存。"""
    client, factory = iso
    sku, session_id = f"OSM-{uuid.uuid4().hex[:8]}", f"sess_osm_{uuid.uuid4().hex[:8]}"
    pid = f"p_{uuid.uuid4().hex[:10]}"
    _seed(factory, session_id, sku, stock=8)
    start = _stock(factory, sku)

    assert _hook(client, "order.paid", pid, sku, session_id).status_code == 200
    for _ in range(3):
        assert _hook(client, "order.refunded", pid, sku, session_id).status_code == 200

    final = _stock(factory, sku)
    _, movements = _restock_movements(factory, pid)
    assert final == start, f"重复退款导致库存虚增：{start} -> {final}（流水 {movements}）"


def test_rest_and_webhook_agree_once_order_left_completed(iso):
    """订单离开 completed 后，REST 与 Webhook 都必须拒绝再次回补。

    这是两条路径的收敛点：修��前 REST 返 409、Webhook 却照扣库存。
    """
    client, factory = iso
    sku, session_id = f"OSM-{uuid.uuid4().hex[:8]}", f"sess_osm_{uuid.uuid4().hex[:8]}"
    pid = f"p_{uuid.uuid4().hex[:10]}"
    _seed(factory, session_id, sku, stock=8)
    start = _stock(factory, sku)

    assert _hook(client, "order.paid", pid, sku, session_id).status_code == 200
    assert _hook(client, "order.cancelled", pid, sku, session_id).status_code == 200
    after_cancel = _stock(factory, sku)

    order_id = _order_id(factory, pid)

    # REST 侧：已取消订单不可退款。
    rest_refund = client.post(
        f"/api/v1/live/orders/{order_id}/refund", json={}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert rest_refund.status_code == 409, f"REST 退款应 409，实际 {rest_refund.status_code}"

    # REST 侧：已取消订单再次取消为幂等 200，但不得回补。
    rest_cancel = client.post(
        f"/api/v1/live/orders/{order_id}/cancel", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert rest_cancel.status_code == 200

    # Webhook 侧：同样不得回补。
    assert _hook(client, "order.refunded", pid, sku, session_id).status_code == 200

    final = _stock(factory, sku)
    _, movements = _restock_movements(factory, pid)
    assert final == start == after_cancel, (
        f"离开 completed 后仍发生回补：{start} -> {final}（流水 {movements}）"
    )
