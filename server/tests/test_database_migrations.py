import asyncio
import uuid
import pytest
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from server.database.migrations import (
    MIGRATIONS,
    run_migrations,
    _column_already_exists,
)


def test_database_migrations_full_lifecycle(tmp_path: Path):
    """测试：空库应用完整版本化迁移以及二次执行的幂等性。"""
    async def _test():
        db_file = tmp_path / f"test_mig_{uuid.uuid4().hex}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

        async with engine.begin() as conn:
            # 1. 模拟历史空库，仅预先创建 orders 和 products 基础表
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS orders (
                    id VARCHAR(64) PRIMARY KEY,
                    product_id VARCHAR(64),
                    user_name VARCHAR(64),
                    amount FLOAT,
                    created_at DATETIME
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS products (
                    id VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    price FLOAT NOT NULL,
                    stock INTEGER NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS live_session_records (
                    id VARCHAR(64) PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS barrage_logs (
                    id VARCHAR(64) PRIMARY KEY,
                    user_name VARCHAR(64),
                    content TEXT
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS voice_profiles (
                    id VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128)
                )
            """))

            # 2. 首次执行完整迁移
            executed = await run_migrations(conn)
            assert executed == len(MIGRATIONS), f"首次迁移应当执行全部 {len(MIGRATIONS)} 个版本"

            # 3. 验证 _schema_migrations 记录表
            res = await conn.execute(text("SELECT version, description FROM _schema_migrations ORDER BY version"))
            rows = res.fetchall()
            assert len(rows) == len(MIGRATIONS)
            recorded_versions = [r[0] for r in rows]
            expected_versions = [m[0] for m in MIGRATIONS]
            assert recorded_versions == expected_versions

            # 4. 验证新增列是否真实创建
            col_res = await conn.execute(text("PRAGMA table_info('orders')"))
            order_cols = {r[1] for r in col_res.fetchall()}
            assert "external_id" in order_cols
            assert "status" in order_cols
            assert "refund_amount" in order_cols

            col_res2 = await conn.execute(text("PRAGMA table_info('products')"))
            product_cols = {r[1] for r in col_res2.fetchall()}
            assert "description" in product_cols
            assert "images" in product_cols
            assert "size_chart" in product_cols

            # 5. 再次执行迁移，验证强幂等性 (应当直接跳过，执行计数为 0)
            executed_again = await run_migrations(conn)
            assert executed_again == 0, "再次执行迁移必须为 0，不应重复执行"

        await engine.dispose()

    asyncio.run(_test())


def test_column_already_exists_detection(tmp_path: Path):
    """测试针对 ALTER TABLE ADD COLUMN 的预检探测。"""
    async def _test():
        db_file = tmp_path / f"test_col_{uuid.uuid4().hex}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE sample_tbl (id VARCHAR(32) PRIMARY KEY, col_a TEXT)"))

            # 已存在的列应返回 True
            exists = await _column_already_exists(conn, "ALTER TABLE sample_tbl ADD COLUMN col_a TEXT")
            assert exists is True

            # 不存在的列应返回 False
            not_exists = await _column_already_exists(conn, "ALTER TABLE sample_tbl ADD COLUMN col_b TEXT")
            assert not_exists is False

            # 非 ALTER ADD COLUMN 语句应返回 False
            other_stmt = await _column_already_exists(conn, "CREATE TABLE other (id INT)")
            assert other_stmt is False

        await engine.dispose()

    asyncio.run(_test())
