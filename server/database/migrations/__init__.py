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
    (
        "0006_avatar_voice_nullable_paths",
        "历史库 avatars/voice_profiles 资产路径列重建为可空（与 ORM 模型一致）",
        [
            # 背景：早期 DDL 将路径列建为 NOT NULL，现行模型为 nullable=True。
            # 启动时的数据清洗迁移会把指向缺失文件的遗留路径置 NULL，旧表约束导致
            # IntegrityError 启动失败。SQLite 不支持 ALTER COLUMN，须整表重建。
            # 两张表在数据库层均无外键引用（已验证现行与历史子表均未建立指向它们的
            # FK 约束），DROP 不会触发级联，重建安全。
            # 前置防御（两层）：
            # 1) CREATE IF NOT EXISTS：对缺失该表的历史空库补建（现行 schema、空表）；
            # 2) ALTER ADD COLUMN：对只有部分列的极老库补齐缺失列（框架预检自动跳过
            #    已存在列；ADD COLUMN 语义即为 nullable）。
            # 随后的整表 rebuild 统一处理"列存在但 NOT NULL"的库（如本次故障库）。
            """
            CREATE TABLE IF NOT EXISTS avatars (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                avatar_type VARCHAR(32),
                source_file_path VARCHAR(512),
                preprocessed_cache_path VARCHAR(512),
                created_at DATETIME
            )
            """,
            "ALTER TABLE avatars ADD COLUMN avatar_type VARCHAR(32)",
            "ALTER TABLE avatars ADD COLUMN source_file_path VARCHAR(512)",
            "ALTER TABLE avatars ADD COLUMN preprocessed_cache_path VARCHAR(512)",
            "ALTER TABLE avatars ADD COLUMN created_at DATETIME",
            """
            CREATE TABLE IF NOT EXISTS voice_profiles (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                sample_wav_path VARCHAR(512),
                embedding_npy_path VARCHAR(512),
                speech_speed FLOAT,
                volume_gain FLOAT,
                created_at DATETIME,
                status VARCHAR(32) DEFAULT 'ready'
            )
            """,
            "ALTER TABLE voice_profiles ADD COLUMN sample_wav_path VARCHAR(512)",
            "ALTER TABLE voice_profiles ADD COLUMN embedding_npy_path VARCHAR(512)",
            "ALTER TABLE voice_profiles ADD COLUMN speech_speed FLOAT",
            "ALTER TABLE voice_profiles ADD COLUMN volume_gain FLOAT",
            "ALTER TABLE voice_profiles ADD COLUMN created_at DATETIME",
            "ALTER TABLE voice_profiles ADD COLUMN status VARCHAR(32) DEFAULT 'ready'",
            "DROP TABLE IF EXISTS avatars_schema_migrate",
            """
            CREATE TABLE avatars_schema_migrate (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                avatar_type VARCHAR(32),
                source_file_path VARCHAR(512),
                preprocessed_cache_path VARCHAR(512),
                created_at DATETIME
            )
            """,
            """
            INSERT INTO avatars_schema_migrate
                (id, name, avatar_type, source_file_path, preprocessed_cache_path, created_at)
            SELECT id, name, avatar_type, source_file_path, preprocessed_cache_path, created_at
            FROM avatars
            """,
            "DROP TABLE avatars",
            "ALTER TABLE avatars_schema_migrate RENAME TO avatars",
            "DROP TABLE IF EXISTS voice_profiles_schema_migrate",
            """
            CREATE TABLE voice_profiles_schema_migrate (
                id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                sample_wav_path VARCHAR(512),
                embedding_npy_path VARCHAR(512),
                speech_speed FLOAT,
                volume_gain FLOAT,
                created_at DATETIME,
                status VARCHAR(32) DEFAULT 'ready'
            )
            """,
            """
            INSERT INTO voice_profiles_schema_migrate
                (id, name, sample_wav_path, embedding_npy_path, speech_speed, volume_gain, created_at, status)
            SELECT id, name, sample_wav_path, embedding_npy_path, speech_speed, volume_gain, created_at, status
            FROM voice_profiles
            """,
            "DROP TABLE voice_profiles",
            "ALTER TABLE voice_profiles_schema_migrate RENAME TO voice_profiles",
        ],
    ),
    (
        "0007_voice_profiles_provider_and_type",
        "voice_profiles 表新增 provider_name(所属引擎)与 voice_type(预设/克隆)",
        [
            "ALTER TABLE voice_profiles ADD COLUMN provider_name VARCHAR(64) DEFAULT ''",
            "ALTER TABLE voice_profiles ADD COLUMN voice_type VARCHAR(32) DEFAULT 'preset'",
        ],
    ),
    (
        "0008_anchors_anchor_type",
        "anchors 表新增 anchor_type (主播类型：ecommerce/entertainment/expert/chat)",
        [
            "ALTER TABLE anchors ADD COLUMN anchor_type VARCHAR(32) DEFAULT 'ecommerce'",
        ],
    ),
    (
        "0009_avatar_tasks_and_anchor_assets",
        "anchors 表新增 avatar_asset_dir 与 source_video，并创建 avatar_tasks 异步任务管理表",
        [
            "ALTER TABLE anchors ADD COLUMN avatar_asset_dir VARCHAR(512) DEFAULT ''",
            "ALTER TABLE anchors ADD COLUMN source_video VARCHAR(512) DEFAULT ''",
            """
            CREATE TABLE IF NOT EXISTS avatar_tasks (
                id VARCHAR(64) PRIMARY KEY,
                anchor_id VARCHAR(64),
                name VARCHAR(128) NOT NULL,
                status VARCHAR(32) DEFAULT 'pending' NOT NULL,
                progress INTEGER DEFAULT 0 NOT NULL,
                stage_message VARCHAR(256) DEFAULT '',
                video_path VARCHAR(512) DEFAULT '',
                output_dir VARCHAR(512) DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(anchor_id) REFERENCES anchors(id) ON DELETE SET NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_avatar_tasks_anchor_id ON avatar_tasks(anchor_id)",
            "CREATE INDEX IF NOT EXISTS ix_avatar_tasks_status ON avatar_tasks(status)",
        ],
    ),
    (
        "0010_avatar_actions",
        "创建 avatar_actions 数字人动作状态机与带货场景绑定表并初始化默认规则",
        [
            """
            CREATE TABLE IF NOT EXISTS avatar_actions (
                id VARCHAR(64) PRIMARY KEY,
                anchor_id VARCHAR(64),
                action_code INTEGER NOT NULL,
                action_name VARCHAR(128) NOT NULL,
                video_path VARCHAR(512) DEFAULT '',
                frames_dir VARCHAR(512) DEFAULT '',
                trigger_type VARCHAR(32) DEFAULT 'both',
                trigger_keywords TEXT DEFAULT '',
                trigger_events VARCHAR(128) DEFAULT '',
                duration_sec FLOAT DEFAULT 3.5,
                priority INTEGER DEFAULT 1,
                mirror_loop INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(anchor_id) REFERENCES anchors(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_avatar_actions_anchor_id ON avatar_actions(anchor_id)",
            "CREATE INDEX IF NOT EXISTS ix_avatar_actions_action_code ON avatar_actions(action_code)",
            """
            INSERT OR IGNORE INTO avatar_actions (id, anchor_id, action_code, action_name, trigger_type, trigger_keywords, trigger_events, duration_sec, priority, mirror_loop, is_active)
            VALUES
                ('action_default_0', NULL, 0, '待机呼吸循环', 'manual', '', '', 0.0, 0, 1, 1),
                ('action_default_1', NULL, 1, '热情挥手欢迎', 'both', '欢迎,来了,刚进,哈喽,晚上好', 'welcome', 3.0, 2, 1, 1),
                ('action_default_2', NULL, 2, '求关注与点赞', 'both', '点赞,关注,粉丝团,灯牌,双击', 'follow', 3.5, 3, 1, 1),
                ('action_default_3', NULL, 3, '促单指引购物车', 'both', '购物车,下单,左下角,抢购,手慢无,买一送,拍下', 'order', 4.0, 5, 1, 1),
                ('action_default_4', NULL, 4, '大额打赏致谢', 'both', '感谢,礼物,破费,大气,老板大气', 'gift', 4.5, 9, 1, 1)
            """,
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
