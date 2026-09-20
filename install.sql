-- ============================================================================
-- AI-LiveStream-Agent 全系统完整数据库初始化脚本 (install.sql)
-- 归档版本：v2.0 (与 server/database/models.py ORM 契约 100% 严格一致)
-- 包含 16 张核心业务与审计流水表，以及默认预设数据。
-- ============================================================================

PRAGMA foreign_keys = ON;

-- 1. 迁移版本审计表
CREATE TABLE IF NOT EXISTS _schema_migrations (
    version VARCHAR(64) PRIMARY KEY,
    description VARCHAR(256),
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 系统通用键值设置表
CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 3. API 服务商与连接参数配置表 (LLM / TTS / Vision / 神经渲染节点等)
CREATE TABLE IF NOT EXISTS api_provider_configs (
    id VARCHAR(64) PRIMARY KEY,
    config_group VARCHAR(32) NOT NULL,
    provider_name VARCHAR(64) NOT NULL,
    is_active INTEGER DEFAULT 0,
    encrypted_api_key TEXT DEFAULT '',
    base_url VARCHAR(512) DEFAULT '',
    model_name VARCHAR(128) DEFAULT '',
    extra_params_json TEXT DEFAULT '{}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 4. 音色资产档案库
CREATE TABLE IF NOT EXISTS voice_profiles (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    sample_wav_path VARCHAR(512) DEFAULT NULL,
    embedding_npy_path VARCHAR(512) DEFAULT NULL,
    speech_speed FLOAT DEFAULT 1.0,
    volume_gain FLOAT DEFAULT 1.0,
    status VARCHAR(32) DEFAULT 'ready',
    provider_name VARCHAR(64) DEFAULT '',
    voice_type VARCHAR(32) DEFAULT 'preset',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 5. 数字人形象配置表
CREATE TABLE IF NOT EXISTS avatars (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    avatar_type VARCHAR(32) DEFAULT 'image',
    source_file_path VARCHAR(512) DEFAULT NULL,
    preprocessed_cache_path VARCHAR(512) DEFAULT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 6. 主播管理表 (形象照片 + 音色绑定 + 资产切片目录 + 原始训练视频)
CREATE TABLE IF NOT EXISTS anchors (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    anchor_type VARCHAR(32) NOT NULL DEFAULT 'ecommerce',
    voice_id VARCHAR(64) DEFAULT NULL,
    remark TEXT DEFAULT '',
    photo_portrait VARCHAR(512) DEFAULT '',
    photo_full_body VARCHAR(512) DEFAULT '',
    photo_half_body VARCHAR(512) DEFAULT '',
    photo_side VARCHAR(512) DEFAULT '',
    avatar_asset_dir VARCHAR(512) DEFAULT '',
    source_video VARCHAR(512) DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (voice_id) REFERENCES voice_profiles(id) ON DELETE SET NULL
);

-- 7. 数字人视频切片与训练异步任务表
CREATE TABLE IF NOT EXISTS avatar_tasks (
    id VARCHAR(64) PRIMARY KEY,
    anchor_id VARCHAR(64) DEFAULT NULL,
    name VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    progress INTEGER NOT NULL DEFAULT 0,
    stage_message VARCHAR(256) DEFAULT '',
    video_path VARCHAR(512) DEFAULT '',
    output_dir VARCHAR(512) DEFAULT '',
    error_message TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (anchor_id) REFERENCES anchors(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_avatar_tasks_anchor_id ON avatar_tasks(anchor_id);
CREATE INDEX IF NOT EXISTS ix_avatar_tasks_status ON avatar_tasks(status);

-- 8. 数字人动作切片状态机与带货场景智能绑定表
CREATE TABLE IF NOT EXISTS avatar_actions (
    id VARCHAR(64) PRIMARY KEY,
    anchor_id VARCHAR(64) DEFAULT NULL,
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
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (anchor_id) REFERENCES anchors(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_avatar_actions_anchor_id ON avatar_actions(anchor_id);
CREATE INDEX IF NOT EXISTS ix_avatar_actions_action_code ON avatar_actions(action_code);

-- 9. 主播角色人设表
CREATE TABLE IF NOT EXISTS anchor_roles (
    id VARCHAR(64) PRIMARY KEY,
    role_type VARCHAR(32) NOT NULL,
    role_name VARCHAR(128) NOT NULL,
    system_prompt TEXT NOT NULL,
    default_voice_id VARCHAR(64) DEFAULT NULL,
    speech_speed FLOAT DEFAULT 1.0,
    pitch_shift FLOAT DEFAULT 0.0,
    associated_guardrail_group VARCHAR(64) DEFAULT 'general',
    is_active INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (default_voice_id) REFERENCES voice_profiles(id) ON DELETE SET NULL
);

-- 10. 电商商品表 (SKU 货盘与逼单话术资产)
CREATE TABLE IF NOT EXISTS products (
    id VARCHAR(64) PRIMARY KEY,
    sku_code VARCHAR(64) UNIQUE NOT NULL,
    title VARCHAR(256) NOT NULL,
    category VARCHAR(64) DEFAULT NULL,
    original_price FLOAT DEFAULT 0.0,
    live_price FLOAT DEFAULT 0.0,
    current_stock INTEGER DEFAULT 0,
    selling_points TEXT DEFAULT '[]',
    faq_data TEXT DEFAULT '[]',
    size_chart TEXT DEFAULT '{}',
    coupon_script TEXT DEFAULT '',
    description TEXT DEFAULT '',
    images TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 1
);

-- 11. 主播违禁词与合规规则表
CREATE TABLE IF NOT EXISTS prohibited_words (
    id VARCHAR(64) PRIMARY KEY,
    word VARCHAR(128) UNIQUE NOT NULL,
    category VARCHAR(32) DEFAULT 'extreme',
    role_scope VARCHAR(32) DEFAULT 'all',
    action_policy VARCHAR(32) DEFAULT 'substitute',
    replacement_word VARCHAR(128) DEFAULT '',
    is_enabled INTEGER DEFAULT 1,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 12. 违禁词触发审计日志表
CREATE TABLE IF NOT EXISTS prohibited_word_logs (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) DEFAULT NULL,
    matched_word VARCHAR(128) NOT NULL,
    category VARCHAR(32) NOT NULL,
    original_sentence TEXT NOT NULL,
    processed_sentence TEXT DEFAULT NULL,
    action_taken VARCHAR(32) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 13. 直播交互与弹幕审计日志表
CREATE TABLE IF NOT EXISTS barrage_logs (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    user_id VARCHAR(64) DEFAULT NULL,
    user_nickname VARCHAR(128) DEFAULT NULL,
    raw_message TEXT NOT NULL,
    event_type VARCHAR(32) DEFAULT 'chat',
    priority_level INTEGER DEFAULT 2,
    ai_reply_text TEXT DEFAULT NULL,
    response_latency_ms INTEGER DEFAULT 0,
    is_interrupted INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 14. 本地 RAG 知识库文档分块表
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id VARCHAR(64) PRIMARY KEY,
    doc_name VARCHAR(256) NOT NULL,
    doc_id VARCHAR(64) NOT NULL,
    chunk_index INTEGER DEFAULT 0,
    content TEXT NOT NULL,
    source_path VARCHAR(512) DEFAULT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_doc_id ON knowledge_chunks(doc_id);

-- 15. 直播场次历史记录表
CREATE TABLE IF NOT EXISTS live_session_records (
    session_id VARCHAR(64) PRIMARY KEY,
    platform VARCHAR(32) DEFAULT 'bilibili',
    theme VARCHAR(128) DEFAULT '',
    mode VARCHAR(16) DEFAULT 'B',
    role_id VARCHAR(64) DEFAULT NULL,
    voice_id VARCHAR(64) DEFAULT NULL,
    anchor_id VARCHAR(64) DEFAULT NULL,
    product_snapshot_json TEXT DEFAULT '[]',
    status VARCHAR(24) NOT NULL DEFAULT 'starting',
    start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    end_time DATETIME DEFAULT NULL,
    total_gmv FLOAT DEFAULT 0.0,
    orders_count INTEGER DEFAULT 0,
    danmaku_count INTEGER DEFAULT 0,
    peak_viewers INTEGER DEFAULT 0,
    gift_income FLOAT DEFAULT 0.0,
    FOREIGN KEY (role_id) REFERENCES anchor_roles(id) ON DELETE SET NULL,
    FOREIGN KEY (voice_id) REFERENCES voice_profiles(id) ON DELETE SET NULL,
    FOREIGN KEY (anchor_id) REFERENCES anchors(id) ON DELETE SET NULL
);

-- 16. 直播成交订单表
CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(64) PRIMARY KEY,
    external_id VARCHAR(128) UNIQUE NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    product_id VARCHAR(64) DEFAULT NULL,
    sku VARCHAR(64) NOT NULL,
    amount FLOAT NOT NULL DEFAULT 0.0,
    quantity INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(24) NOT NULL DEFAULT 'completed',
    refund_amount FLOAT NOT NULL DEFAULT 0.0,
    note TEXT DEFAULT '',
    product_snapshot_json TEXT DEFAULT '{}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES live_session_records(session_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_session_id ON orders(session_id);
CREATE INDEX IF NOT EXISTS ix_orders_external_id ON orders(external_id);

-- 17. 库存变动审计流水表
CREATE TABLE IF NOT EXISTS inventory_movements (
    id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL,
    product_id VARCHAR(64) NOT NULL,
    quantity_delta INTEGER NOT NULL,
    reason VARCHAR(32) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_inventory_movements_order_id ON inventory_movements(order_id);
CREATE INDEX IF NOT EXISTS ix_inventory_movements_product_id ON inventory_movements(product_id);

-- ============================================================================
-- 预设基础种子数据 (默认角色、违禁词与动作状态机规则)
-- ============================================================================

INSERT OR IGNORE INTO avatar_actions (id, anchor_id, action_code, action_name, trigger_type, trigger_keywords, trigger_events, duration_sec, priority, mirror_loop, is_active)
VALUES
    ('action_default_0', NULL, 0, '待机呼吸循环', 'manual', '', '', 0.0, 0, 1, 1),
    ('action_default_1', NULL, 1, '热情挥手欢迎', 'both', '欢迎,来了,刚进,哈喽,晚上好', 'welcome', 3.0, 2, 1, 1),
    ('action_default_2', NULL, 2, '求关注与点赞', 'both', '点赞,关注,粉丝团,灯牌,双击', 'follow', 3.5, 3, 1, 1),
    ('action_default_3', NULL, 3, '促单指引购物车', 'both', '购物车,下单,左下角,抢购,手慢无,买一送,拍下', 'order', 4.0, 5, 1, 1),
    ('action_default_4', NULL, 4, '大额打赏致谢', 'both', '感谢,礼物,破费,大气,老板大气', 'gift', 4.5, 9, 1, 1);

INSERT OR IGNORE INTO prohibited_words (id, word, category, role_scope, action_policy, replacement_word, is_enabled)
VALUES
    ('pw_1', '全网第一', 'extreme', 'all', 'substitute', '深受大家喜爱', 1),
    ('pw_2', '最顶尖', 'extreme', 'all', 'substitute', '非常出色', 1),
    ('pw_3', '秒杀', 'extreme', 'ecommerce', 'substitute', '限时抢购', 1),
    ('pw_4', '最好', 'extreme', 'all', 'substitute', '深得好评', 1),
    ('pw_5', '纯天然无毒副作用', 'medical', 'all', 'substitute', '甄选自然健康配料', 1),
    ('pw_6', '根治', 'medical', 'all', 'drop', '', 1),
    ('pw_7', '包治百病', 'medical', 'all', 'drop', '', 1),
    ('pw_8', '加微信', 'traffic', 'all', 'substitute', '关注直播间或私信', 1),
    ('pw_9', '私下转账', 'traffic', 'all', 'drop', '', 1),
    ('pw_10', '包胜诉', 'expert', 'expert', 'drop', '', 1);

-- ============================================================================
-- 增量数据库结构升级脚本 (旧库执行升级迁移用)
-- ============================================================================
-- 若从旧版本数据库升级，请依次执行以下语句：
-- ALTER TABLE voice_profiles ADD COLUMN provider_name VARCHAR(64) DEFAULT '';
-- ALTER TABLE voice_profiles ADD COLUMN voice_type VARCHAR(32) DEFAULT 'preset';
-- ALTER TABLE anchors ADD COLUMN anchor_type VARCHAR(32) DEFAULT 'ecommerce';
-- ALTER TABLE anchors ADD COLUMN avatar_asset_dir VARCHAR(512) DEFAULT '';
-- ALTER TABLE anchors ADD COLUMN source_video VARCHAR(512) DEFAULT '';
-- ALTER TABLE barrage_logs ADD COLUMN is_interrupted INTEGER DEFAULT 0;
-- ALTER TABLE live_session_records ADD COLUMN mode VARCHAR(16) DEFAULT 'B';
-- ALTER TABLE live_session_records ADD COLUMN theme VARCHAR(128) DEFAULT '';
-- ALTER TABLE live_session_records ADD COLUMN role_id VARCHAR(64);
-- ALTER TABLE live_session_records ADD COLUMN product_snapshot_json TEXT DEFAULT '[]';
-- CREATE TABLE IF NOT EXISTS avatar_tasks (
--     id VARCHAR(64) PRIMARY KEY,
--     anchor_id VARCHAR(64) DEFAULT NULL,
--     name VARCHAR(128) NOT NULL,
--     status VARCHAR(32) NOT NULL DEFAULT 'pending',
--     progress INTEGER NOT NULL DEFAULT 0,
--     stage_message VARCHAR(256) DEFAULT '',
--     video_path VARCHAR(512) DEFAULT '',
--     output_dir VARCHAR(512) DEFAULT '',
--     error_message TEXT DEFAULT '',
--     created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
--     updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
--     FOREIGN KEY (anchor_id) REFERENCES anchors(id) ON DELETE SET NULL
-- );
-- CREATE INDEX IF NOT EXISTS ix_avatar_tasks_anchor_id ON avatar_tasks(anchor_id);
-- CREATE INDEX IF NOT EXISTS ix_avatar_tasks_status ON avatar_tasks(status);
-- CREATE TABLE IF NOT EXISTS avatar_actions (
--     id VARCHAR(64) PRIMARY KEY,
--     anchor_id VARCHAR(64) DEFAULT NULL,
--     action_code INTEGER NOT NULL,
--     action_name VARCHAR(128) NOT NULL,
--     video_path VARCHAR(512) DEFAULT '',
--     frames_dir VARCHAR(512) DEFAULT '',
--     trigger_type VARCHAR(32) DEFAULT 'both',
--     trigger_keywords TEXT DEFAULT '',
--     trigger_events VARCHAR(128) DEFAULT '',
--     duration_sec FLOAT DEFAULT 3.5,
--     priority INTEGER DEFAULT 1,
--     mirror_loop INTEGER DEFAULT 1,
--     is_active INTEGER DEFAULT 1,
--     created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
--     updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
--     FOREIGN KEY (anchor_id) REFERENCES anchors(id) ON DELETE CASCADE
-- );
-- CREATE INDEX IF NOT EXISTS ix_avatar_actions_anchor_id ON avatar_actions(anchor_id);
-- CREATE INDEX IF NOT EXISTS ix_avatar_actions_action_code ON avatar_actions(action_code);

