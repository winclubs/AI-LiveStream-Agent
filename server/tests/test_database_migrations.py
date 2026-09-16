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


def test_migration_0006_rebuild_legacy_not_null_path_columns(tmp_path: Path):
    """历史库 avatars/voice_profiles 路径列为 NOT NULL 时，0006 必须重建为可空并保留数据。

    复现生产启动崩溃：数据清洗迁移把指向缺失文件的遗留路径置 NULL，
    旧表 NOT NULL 约束导致 IntegrityError。
    """
    async def _test():
        db_file = tmp_path / f"test_mig_legacy_{uuid.uuid4().hex}.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

        async with engine.begin() as conn:
            # 模拟旧版本基础表 (0002-0005 迁移的执行前提)
            await conn.execute(text("""
                CREATE TABLE orders (
                    id VARCHAR(64) PRIMARY KEY,
                    product_id VARCHAR(64),
                    user_name VARCHAR(64),
                    amount FLOAT,
                    created_at DATETIME
                )
            """))
            await conn.execute(text("""
                CREATE TABLE products (
                    id VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    price FLOAT NOT NULL,
                    stock INTEGER NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE live_session_records (
                    id VARCHAR(64) PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE TABLE barrage_logs (
                    id VARCHAR(64) PRIMARY KEY,
                    user_name VARCHAR(64),
                    content TEXT
                )
            """))
            # 旧 schema：路径列 NOT NULL，与现行 ORM (nullable=True) 漂移
            await conn.execute(text("""
                CREATE TABLE avatars (
                    id VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    avatar_type VARCHAR(32),
                    source_file_path VARCHAR(512) NOT NULL,
                    preprocessed_cache_path VARCHAR(512),
                    created_at DATETIME
                )
            """))
            await conn.execute(text("""
                CREATE TABLE voice_profiles (
                    id VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    sample_wav_path VARCHAR(512) NOT NULL,
                    embedding_npy_path VARCHAR(512),
                    speech_speed FLOAT,
                    volume_gain FLOAT,
                    created_at DATETIME,
                    status VARCHAR(32) DEFAULT 'ready'
                )
            """))
            # 遗留数据：内置伪默认记录 + 用户自定义记录
            await conn.execute(text("""
                INSERT INTO avatars (id, name, avatar_type, source_file_path, preprocessed_cache_path)
                VALUES ('avatar_default_muse', 'legacy-default', 'image',
                        'uploads/avatars/missing.png', 'uploads/avatars/missing.pkl')
            """))
            await conn.execute(text("""
                INSERT INTO avatars (id, name, avatar_type, source_file_path, preprocessed_cache_path)
                VALUES ('avatar_custom', 'user-avatar', 'image',
                        'data/avatars/custom.png', 'data/avatars/custom.pkl')
            """))
            await conn.execute(text("""
                INSERT INTO voice_profiles (id, name, sample_wav_path, embedding_npy_path, speech_speed, volume_gain, status)
                VALUES ('voice_default_female', 'legacy-voice', 'uploads/voices/missing.wav', 'uploads/voices/missing.npy', 1.0, 1.0, 'ready')
            """))

            executed = await run_migrations(conn)
            assert executed == len(MIGRATIONS), "首次迁移应执行全部版本"

            # 路径列必须已重建为 nullable (notnull=0)
            for table, column in (
                ("avatars", "source_file_path"),
                ("avatars", "preprocessed_cache_path"),
                ("voice_profiles", "sample_wav_path"),
                ("voice_profiles", "embedding_npy_path"),
            ):
                res = await conn.execute(text(f"PRAGMA table_info({table})"))
                notnull = {row[1]: row[3] for row in res.fetchall()}[column]
                assert notnull == 0, f"{table}.{column} 迁移后必须允许 NULL"

            # 数据必须完整保留
            res = await conn.execute(text(
                "SELECT id, source_file_path FROM avatars ORDER BY id"
            ))
            rows = res.fetchall()
            assert rows == [
                ("avatar_custom", "data/avatars/custom.png"),
                ("avatar_default_muse", "uploads/avatars/missing.png"),
            ]
            res = await conn.execute(text(
                "SELECT id, sample_wav_path, speech_speed, status FROM voice_profiles"
            ))
            voice_row = res.fetchone()
            assert voice_row == ("voice_default_female", "uploads/voices/missing.wav", 1.0, "ready")

            # 复现原启动崩溃的 UPDATE：现在必须成功
            await conn.execute(text(
                "UPDATE avatars SET source_file_path=NULL, preprocessed_cache_path=NULL "
                "WHERE id='avatar_default_muse'"
            ))
            await conn.execute(text(
                "UPDATE voice_profiles SET sample_wav_path=NULL, embedding_npy_path=NULL "
                "WHERE id='voice_default_female'"
            ))

            # 二次执行幂等
            assert await run_migrations(conn) == 0

        await engine.dispose()

    asyncio.run(_test())
