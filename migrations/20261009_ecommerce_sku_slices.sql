-- ==============================================================================
-- 数据库升级迁移脚本: 20261009_ecommerce_sku_slices.sql
-- 适用阶段：动静分离 / 批播结合架构之电商商品高精预渲染切片资产库
-- 包含：ecommerce_sku_slices 表结构定义、索引及版本审计记录
-- 兼容 SQLite / MySQL
-- ==============================================================================

CREATE TABLE IF NOT EXISTS ecommerce_sku_slices (
    id VARCHAR(64) PRIMARY KEY,
    product_id VARCHAR(64) NOT NULL,
    slot_code VARCHAR(32) NOT NULL,
    slice_type VARCHAR(32) DEFAULT 'main_pitch',
    script_text TEXT NOT NULL,
    video_path VARCHAR(512) NOT NULL,
    audio_path VARCHAR(512) NOT NULL,
    duration_sec FLOAT NOT NULL,
    coords_path VARCHAR(512) DEFAULT NULL,
    loudness_lufs FLOAT DEFAULT -16.0,
    bg_noise_profile TEXT DEFAULT '{}',
    render_engine VARCHAR(32) DEFAULT 'latentsync_cloud',
    is_active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ecommerce_sku_slices_product_id ON ecommerce_sku_slices(product_id);
CREATE INDEX IF NOT EXISTS ix_ecommerce_sku_slices_slot_code ON ecommerce_sku_slices(slot_code);

INSERT OR IGNORE INTO _schema_migrations (version, description)
VALUES ('0013_create_ecommerce_sku_slices', '创建电商商品高精预渲染切片表 (ecommerce_sku_slices)');
