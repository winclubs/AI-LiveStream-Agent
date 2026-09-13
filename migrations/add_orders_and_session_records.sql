-- ==============================================================================
-- 数据库升级迁移脚本: add_orders_and_session_records.sql
-- 用于在已有数据库中增量创建 orders 表与 live_session_records 表
-- 兼容 SQLite / MySQL
-- ==============================================================================

-- 1. 创建直播成交订单表
CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    product_id VARCHAR(64),
    sku VARCHAR(64),
    amount FLOAT NOT NULL DEFAULT 0.0,
    quantity INTEGER DEFAULT 1,
    note TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_session_id ON orders (session_id);

-- 2. 创建直播场次历史记录表
CREATE TABLE IF NOT EXISTS live_session_records (
    session_id VARCHAR(64) PRIMARY KEY,
    platform VARCHAR(32) DEFAULT 'bilibili',
    theme VARCHAR(128) DEFAULT '',
    mode VARCHAR(16) DEFAULT 'B',
    voice_id VARCHAR(64) DEFAULT '',
    anchor_id VARCHAR(64) DEFAULT '',
    start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    end_time DATETIME,
    total_gmv FLOAT DEFAULT 0.0,
    orders_count INTEGER DEFAULT 0,
    danmaku_count INTEGER DEFAULT 0,
    peak_viewers INTEGER DEFAULT 0,
    gift_income FLOAT DEFAULT 0.0
);
