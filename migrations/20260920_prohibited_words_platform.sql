-- ============================================================================
-- 数据库增量升级脚本: 20260920_prohibited_words_platform.sql
-- 针对 prohibited_words 违禁词表增加 platform 平台维度字段 (all/douyin/wechat/kuaishou/bilibili)
-- ============================================================================

ALTER TABLE prohibited_words ADD COLUMN platform VARCHAR(32) DEFAULT 'all';

CREATE INDEX IF NOT EXISTS ix_prohibited_words_platform ON prohibited_words(platform);

-- 针对存量历史数据，如为空则兜底填充为全平台通用规则 'all'
UPDATE prohibited_words SET platform = 'all' WHERE platform IS NULL OR platform = '';
