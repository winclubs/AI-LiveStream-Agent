-- ============================================================================
-- 数据库增量升级脚本: 20260917_avatar_tasks.sql
-- 适用版本: v1.9.0
-- 变更说明:
--   1. anchors 表新增 avatar_asset_dir 与 source_video 字段，记录数字人切片资产目录与源视频
--   2. 新增 avatar_tasks 表，用于异步记录与轮询数字人视频切片制作任务的进度与状态
-- ============================================================================

-- 1. 为 anchors 表增加切片资产与源视频字段
ALTER TABLE anchors ADD COLUMN avatar_asset_dir VARCHAR(512) DEFAULT '';
ALTER TABLE anchors ADD COLUMN source_video VARCHAR(512) DEFAULT '';

-- 2. 创建 avatar_tasks 异步切片任务管理表
CREATE TABLE IF NOT EXISTS avatar_tasks (
    id VARCHAR(64) PRIMARY KEY,
    anchor_id VARCHAR(64) DEFAULT NULL,
    name VARCHAR(128) NOT NULL,
    status VARCHAR(32) DEFAULT 'pending',
    progress INTEGER DEFAULT 0,
    stage_message VARCHAR(256) DEFAULT '',
    video_path VARCHAR(512) DEFAULT '',
    output_dir VARCHAR(512) DEFAULT '',
    error_message TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (anchor_id) REFERENCES anchors(id) ON DELETE SET NULL
);

-- 3. 创建必要索引
CREATE INDEX IF NOT EXISTS ix_avatar_tasks_anchor_id ON avatar_tasks(anchor_id);
CREATE INDEX IF NOT EXISTS ix_avatar_tasks_status ON avatar_tasks(status);

-- 4. 登记迁移历史版本记录
INSERT OR IGNORE INTO _schema_migrations (version, description)
VALUES ('0009_avatar_tasks_and_anchor_assets', 'anchors 表新增 avatar_asset_dir 与 source_video，并创建 avatar_tasks 异步任务管理表');
