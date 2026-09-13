"""
SQLite 轻量级版本化迁移框架
- 使用 _schema_migrations 表记录已应用版本
- create_all 只能新建缺失表，无法处理历史库的列/索引变更，本框架补足升级路径
"""
import logging
import re
from sqlalchemy import text
from server.database.models import Base

logger = logging.getLogger("LiveAgent.Migrations")

# 迁移脚本注册表：(版本号, 描述, SQL 列表)
# 注意：所有 SQL 必须幂等 (IF NOT EXISTS / 容错)，保证新旧库均可安全执行
MIGRATIONS = [
    (
        "0001_create_knowledge_chunks",
        "创建本地 RAG 知识库分块表",
        [
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id VARCHAR(64) PRIMARY KEY,
                doc_name VARCHAR(256) NOT NULL,
                doc_id VARCHAR(64) NOT NULL,
                chunk_index INTEGER,
                content TEXT NOT NULL,
                source_path VARCHAR(512),
                created_at DATETIME
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_doc_id ON knowledge_chunks (doc_id)",
        ],
    ),
    (
        "0002_barrage_log_reply_columns",
        "弹幕审计日志补充 AI 回复与响应延迟列",
        [
            "ALTER TABLE barrage_logs ADD COLUMN ai_reply_text TEXT",
            "ALTER TABLE barrage_logs ADD COLUMN response_latency_ms INTEGER",
        ],
    ),
    (
        "0003_anchors_appsettings_product_images_voice_status",
        "主播管理表/应用设置表/商品图片与描述/音色克隆状态",
        [
            """
            CREATE TABLE IF NOT EXISTS anchors (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                voice_id VARCHAR(64),
                remark TEXT,
                photo_portrait VARCHAR(512),
                photo_full_body VARCHAR(512),
                photo_half_body VARCHAR(512),
                photo_side VARCHAR(512),
                created_at DATETIME
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR(64) PRIMARY KEY,
                value TEXT,
                updated_at DATETIME
            )
            """,
            "ALTER TABLE products ADD COLUMN description TEXT",
            "ALTER TABLE products ADD COLUMN images TEXT",
            "ALTER TABLE products ADD COLUMN size_chart TEXT DEFAULT '{}'",
            "ALTER TABLE voice_profiles ADD COLUMN status VARCHAR(32) DEFAULT 'ready'",
        ],
    ),
    (
        "0004_order_session_reliability",
        "订单幂等、状态、场次快照与库存流水",
        [
            "ALTER TABLE orders ADD COLUMN external_id VARCHAR(128)",
            "ALTER TABLE orders ADD COLUMN status VARCHAR(24) NOT NULL DEFAULT 'completed'",
            "ALTER TABLE orders ADD COLUMN refund_amount FLOAT NOT NULL DEFAULT 0.0",
            "ALTER TABLE orders ADD COLUMN product_snapshot_json TEXT DEFAULT '{}'",
            "ALTER TABLE orders ADD COLUMN updated_at DATETIME",
            "ALTER TABLE live_session_records ADD COLUMN role_id VARCHAR(64)",
            "ALTER TABLE live_session_records ADD COLUMN product_snapshot_json TEXT DEFAULT '[]'",
            "ALTER TABLE live_session_records ADD COLUMN status VARCHAR(24) NOT NULL DEFAULT 'stopped'",
            "UPDATE orders SET external_id = id WHERE external_id IS NULL OR external_id = ''",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_external_id ON orders(external_id)",
            """
            CREATE TABLE IF NOT EXISTS inventory_movements (
                id VARCHAR(64) PRIMARY KEY,
                order_id VARCHAR(64) NOT NULL,
                product_id VARCHAR(64) NOT NULL,
                quantity_delta INTEGER NOT NULL,
                reason VARCHAR(32) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_inventory_movements_order_id ON inventory_movements(order_id)",
            "CREATE INDEX IF NOT EXISTS ix_inventory_movements_product_id ON inventory_movements(product_id)",
        ],
    ),
    (
        "0005_repair_product_size_chart",
        "修复历史商品表缺失尺码表字段",
        [
            "ALTER TABLE products ADD COLUMN size_chart TEXT DEFAULT '{}'",
        ],
    ),
]


_ALTER_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+([\w\"]+)\s+ADD\s+COLUMN\s+([\w\"]+)", re.IGNORECASE
)


async def _column_already_exists(conn, statement: str) -> bool:
    """SQLite 不支持所有版本的 ADD COLUMN IF NOT EXISTS，执行前显式检查。"""
    match = _ALTER_ADD_COLUMN_RE.match(statement)
    if not match or conn.dialect.name != "sqlite":
        return False
    table = match.group(1).strip('"')
    column = match.group(2).strip('"')
    result = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    return any(row[1] == column for row in result.fetchall())


async def run_migrations(conn) -> int:
    """
    按顺序执行未应用的迁移脚本
    :param conn: SQLAlchemy AsyncConnection (已在事务/连接上下文中)
    :return: 本次执行的迁移数量
    """
    # 1. 确保迁移记录表存在
    await conn.execute(text(
        "CREATE TABLE IF NOT EXISTS _schema_migrations ("
        " version VARCHAR(64) PRIMARY KEY,"
        " description VARCHAR(256),"
        " applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    ))

    # 2. 读取已应用版本
    result = await conn.execute(text("SELECT version FROM _schema_migrations"))
    applied = {row[0] for row in result.fetchall()}

    # 3. 依次执行未应用的迁移
    executed = 0
    for version, description, statements in MIGRATIONS:
        if version in applied:
            continue
        for stmt in statements:
            if await _column_already_exists(conn, stmt):
                logger.debug("迁移 %s 已存在目标列，跳过幂等语句", version)
                continue
            # 非预期错误必须传播给外层事务，版本记录不得前进。
            await conn.execute(text(stmt))
        await conn.execute(
            text("INSERT INTO _schema_migrations (version, description) VALUES (:v, :d)"),
            {"v": version, "d": description}
        )
        executed += 1
        logger.info(f"数据库迁移已应用: {version} - {description}")

    if executed:
        logger.info(f"共执行 {executed} 个数据库迁移脚本")
    return executed
