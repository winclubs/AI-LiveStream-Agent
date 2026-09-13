import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from server.app import app
from server.core.roles.role_manager import global_role_manager
from server.database.db import AsyncSessionLocal, get_db
from server.database.models import (
    AnchorRole,
    Base,
    InventoryMovement,
    LiveSessionRecord,
    Order,
    Product,
)
from server.database import migrations as migration_module
from server.routes.live import global_live_controller


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/v1/live/stop")
        yield test_client
        test_client.post("/api/v1/live/stop")


@pytest.fixture()
def isolated_business_client(tmp_path):
    """共享一个 TestClient，并让目标业务请求使用独立的 SQLite 数据库。"""
    db_path = tmp_path / "business_reliability.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    isolated_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    active_role = global_role_manager.get_active_role()

    async def setup_database():
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await connection.run_sync(Base.metadata.create_all)
        async with isolated_session() as session:
            session.add(AnchorRole(
                id=active_role.role_id,
                role_type=active_role.role_type,
                role_name=active_role.role_name,
                system_prompt=active_role.system_prompt,
                speech_speed=active_role.speech_speed,
                pitch_shift=active_role.pitch_shift,
                associated_guardrail_group=active_role.guardrail_profile,
                is_active=1,
            ))
            await session.commit()

    async def isolated_get_db():
        async with isolated_session() as session:
            yield session

    asyncio.run(setup_database())
    app.dependency_overrides[get_db] = isolated_get_db
    try:
        with TestClient(app) as test_client:
            test_client.post("/api/v1/live/stop")
            yield test_client, isolated_session
            test_client.post("/api/v1/live/stop")
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def _post_concurrently(client, path, *, json=None):
    barrier = threading.Barrier(2)

    def post_once():
        barrier.wait(timeout=5)
        return client.post(path, json=json)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(post_once) for _ in range(2)]
        return [future.result(timeout=10) for future in futures]


def test_role_upsert_does_not_activate_and_switch_is_persisted(client):
    before = client.get("/api/v1/roles/list").json()["active_role_id"]
    role_id = f"role_test_{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/v1/roles/upsert",
        json={
            "id": role_id,
            "role_type": "chitchat",
            "role_name": "持久化测试主播",
            "system_prompt": "只用于测试角色持久化。",
        },
    )
    assert response.status_code == 200
    assert client.get("/api/v1/roles/list").json()["active_role_id"] == before

    switched = client.post("/api/v1/roles/switch", json={"role_id": role_id})
    assert switched.status_code == 200

    async def verify():
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(AnchorRole))).scalars().all()
            active = [row.id for row in rows if row.is_active]
            assert active == [role_id]

    asyncio.run(verify())

    # 模拟进程重启时运行态角色注册表被重建，再从数据库恢复。
    global_role_manager.reset_to_defaults()
    asyncio.run(global_role_manager.load_from_db())
    assert global_role_manager.get_active_role().role_id == role_id


def test_custom_role_can_be_loaded_directly_by_live_start(client):
    role_id = f"role_direct_{uuid.uuid4().hex[:8]}"
    client.post(
        "/api/v1/roles/upsert",
        json={
            "id": role_id,
            "role_type": "entertainment",
            "role_name": "直接开播测试主播",
            "system_prompt": "直接开播测试。",
        },
    )
    global_role_manager.reset_to_defaults()

    response = client.post(
        "/api/v1/live/start",
        json={"room_id": "mock", "platform": "mock", "role_id": role_id},
    )
    assert response.status_code == 200
    assert response.json()["role_id"] == role_id


def test_order_idempotency_stock_and_history(client):
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    product_response = client.post(
        "/api/v1/products/upsert",
        json={
            "sku_code": sku,
            "title": "订单可靠性测试商品",
            "live_price": 19.9,
            "current_stock": 3,
            "selling_points": ["稳定", "可追溯"],
        },
    )
    assert product_response.status_code == 200

    started = client.post("/api/v1/live/start", json={"room_id": "mock", "platform": "mock"})
    assert started.status_code == 200
    session_id = started.json()["session_id"]
    external_id = f"manual-{uuid.uuid4().hex}"

    payload = {
        "external_id": external_id,
        "amount": 39.8,
        "sku": sku,
        "quantity": 2,
    }
    first = client.post("/api/v1/live/stats/order", json=payload)
    assert first.status_code == 200
    second = client.post("/api/v1/live/stats/order", json=payload)
    assert second.status_code == 200
    assert second.json()["data"]["order_id"] == first.json()["data"]["order_id"]
    assert second.json()["data"]["idempotent_replay"] is True

    orders = client.get(f"/api/v1/live/orders?session_id={session_id}")
    assert orders.status_code == 200
    assert len([item for item in orders.json()["data"] if item["external_id"] == external_id]) == 1

    products = client.get("/api/v1/products/list").json()["data"]
    product = next(item for item in products if item["sku_code"] == sku)
    assert product["current_stock"] == 1

    order_id = first.json()["data"]["order_id"]
    cancelled = client.post(f"/api/v1/live/orders/{order_id}/cancel")
    assert cancelled.status_code == 200
    products = client.get("/api/v1/products/list").json()["data"]
    product = next(item for item in products if item["sku_code"] == sku)
    assert product["current_stock"] == 3

    client.post("/api/v1/live/stop")
    sessions = client.get("/api/v1/live/sessions")
    assert sessions.status_code == 200
    row = next(item for item in sessions.json()["data"] if item["session_id"] == session_id)
    assert row["status"] == "stopped"
    assert row["orders_count"] == 0
    assert row["total_gmv"] == 0.0


def test_unknown_and_insufficient_stock_orders_are_rejected(client):
    client.post("/api/v1/live/start", json={"room_id": "mock", "platform": "mock"})
    missing = client.post(
        "/api/v1/live/stats/order",
        json={"amount": 1, "sku": "SKU-DOES-NOT-EXIST", "quantity": 1},
    )
    assert missing.status_code == 404

    sku = f"EMPTY-{uuid.uuid4().hex[:8]}"
    client.post(
        "/api/v1/products/upsert",
        json={"sku_code": sku, "title": "零库存商品", "live_price": 1, "current_stock": 0},
    )
    empty = client.post(
        "/api/v1/live/stats/order",
        json={"amount": 1, "sku": sku, "quantity": 1},
    )
    assert empty.status_code == 409


def test_concurrent_live_start_has_one_winner_and_no_ghost_session(
    isolated_business_client, monkeypatch
):
    test_client, isolated_session = isolated_business_client
    original_start = global_live_controller.start

    async def widen_preexisting_race(*args, **kwargs):
        # 旧实现中让两个请求都越过 is_live 检查；加锁后第二个请求不会进入这里。
        await asyncio.sleep(0.1)
        return await original_start(*args, **kwargs)

    monkeypatch.setattr(global_live_controller, "start", widen_preexisting_race)
    responses = _post_concurrently(
        test_client,
        "/api/v1/live/start",
        json={"room_id": "mock", "platform": "mock"},
    )
    assert sorted(response.status_code for response in responses) == [200, 409]

    winner_session_id = next(
        response.json()["session_id"] for response in responses if response.status_code == 200
    )

    async def verify_single_active_session():
        async with isolated_session() as session:
            active = (
                await session.execute(
                    select(LiveSessionRecord).where(
                        LiveSessionRecord.status.in_(["starting", "live"])
                    )
                )
            ).scalars().all()
            assert [(row.session_id, row.status) for row in active] == [
                (winner_session_id, "live")
            ]

    asyncio.run(verify_single_active_session())
    assert global_live_controller.session_id == winner_session_id


def test_concurrent_completed_order_refund_has_one_winner_and_one_restock(
    isolated_business_client, monkeypatch
):
    test_client, isolated_session = isolated_business_client
    sku = f"REFUND-{uuid.uuid4().hex[:8]}"
    created = test_client.post(
        "/api/v1/products/upsert",
        json={"sku_code": sku, "title": "并发退款商品", "live_price": 20, "current_stock": 5},
    )
    assert created.status_code == 200
    assert test_client.post(
        "/api/v1/live/start", json={"room_id": "mock", "platform": "mock"}
    ).status_code == 200
    sold = test_client.post(
        "/api/v1/live/stats/order",
        json={
            "external_id": f"refund-{uuid.uuid4().hex}",
            "amount": 40,
            "sku": sku,
            "quantity": 2,
        },
    )
    assert sold.status_code == 200
    order_id = sold.json()["data"]["order_id"]

    original_execute = AsyncSession.execute
    gate = {"enabled": True, "count": 0, "event": None}

    async def synchronize_refund_reads(session, statement, *args, **kwargs):
        result = await original_execute(session, statement, *args, **kwargs)
        sql = str(statement)
        if gate["enabled"] and "FROM orders" in sql and "orders.id =" in sql:
            gate["count"] += 1
            if gate["event"] is None:
                gate["event"] = asyncio.Event()
            if gate["count"] == 2:
                gate["event"].set()
            await asyncio.wait_for(gate["event"].wait(), timeout=3)
        return result

    monkeypatch.setattr(AsyncSession, "execute", synchronize_refund_reads)
    responses = _post_concurrently(
        test_client, f"/api/v1/live/orders/{order_id}/refund", json={}
    )
    gate["enabled"] = False
    assert sorted(response.status_code for response in responses) == [200, 409]

    async def verify_refund_side_effects():
        async with isolated_session() as session:
            product = (
                await session.execute(select(Product).where(Product.sku_code == sku))
            ).scalar_one()
            order = await session.get(Order, order_id)
            refund_count = (
                await session.execute(
                    select(func.count(InventoryMovement.id)).where(
                        InventoryMovement.order_id == order_id,
                        InventoryMovement.reason == "refund",
                    )
                )
            ).scalar_one()
            assert product.current_stock == 5
            assert order.status == "refunded"
            assert order.refund_amount == 40
            assert refund_count == 1

    asyncio.run(verify_refund_side_effects())


def test_historical_external_id_replay_does_not_pollute_current_session_stats(
    isolated_business_client,
):
    test_client, _isolated_session = isolated_business_client
    sku = f"REPLAY-{uuid.uuid4().hex[:8]}"
    assert test_client.post(
        "/api/v1/products/upsert",
        json={"sku_code": sku, "title": "跨场重放商品", "live_price": 10, "current_stock": 10},
    ).status_code == 200

    session_a = test_client.post(
        "/api/v1/live/start", json={"room_id": "mock", "platform": "mock"}
    )
    assert session_a.status_code == 200
    historical_external_id = f"history-{uuid.uuid4().hex}"
    assert test_client.post(
        "/api/v1/live/stats/order",
        json={"external_id": historical_external_id, "amount": 20, "sku": sku, "quantity": 2},
    ).status_code == 200
    assert test_client.post("/api/v1/live/stop").status_code == 200

    session_b = test_client.post(
        "/api/v1/live/start", json={"room_id": "mock", "platform": "mock"}
    )
    assert session_b.status_code == 200
    assert test_client.post(
        "/api/v1/live/stats/order",
        json={
            "external_id": f"current-{uuid.uuid4().hex}",
            "amount": 7,
            "sku": sku,
            "quantity": 1,
        },
    ).status_code == 200

    replay = test_client.post(
        "/api/v1/live/stats/order",
        json={"external_id": historical_external_id, "amount": 20, "sku": sku, "quantity": 2},
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["idempotent_replay"] is True
    assert replay.json()["data"]["gmv_yuan"] == 20
    assert replay.json()["data"]["orders_count"] == 1

    current_stats = test_client.get("/api/v1/live/stats").json()["data"]
    assert current_stats["gmv_yuan"] == 7
    assert current_stats["orders_count"] == 1


def test_migration_failure_is_not_recorded(tmp_path, monkeypatch):
    db_path = tmp_path / "migration_failure.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    broken = [("9999_broken", "故障注入", ["CREATE TABLE valid_part (id INTEGER)", "INVALID SQL"])]
    monkeypatch.setattr(migration_module, "MIGRATIONS", broken)

    async def exercise():
        with pytest.raises(Exception):
            async with engine.begin() as connection:
                await migration_module.run_migrations(connection)
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT COUNT(*) FROM _schema_migrations WHERE version='9999_broken'")
            )
            assert result.scalar_one() == 0
        await engine.dispose()

    asyncio.run(exercise())
