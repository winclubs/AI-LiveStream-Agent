-- ==============================================================================
-- 数据库升级迁移脚本: 20260925_add_oss_and_live_recordings.sql
-- 用于在已有 live_session_records 表中增量追加直播录像路径、时长与阿里云OSS云端归档字段
-- 兼容 SQLite / MySQL
-- ==============================================================================

ALTER TABLE live_session_records ADD COLUMN video_path VARCHAR(512) DEFAULT '';
ALTER TABLE live_session_records ADD COLUMN oss_url VARCHAR(1024) DEFAULT '';
ALTER TABLE live_session_records ADD COLUMN preview_image_url VARCHAR(1024) DEFAULT '';
ALTER TABLE live_session_records ADD COLUMN video_duration_sec FLOAT DEFAULT 0.0;
ALTER TABLE live_session_records ADD COLUMN video_size_bytes BIGINT DEFAULT 0;
ALTER TABLE live_session_records ADD COLUMN oss_upload_status VARCHAR(32) DEFAULT 'none';
ALTER TABLE live_session_records ADD COLUMN oss_error_msg TEXT DEFAULT '';
