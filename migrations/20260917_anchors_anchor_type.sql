-- ============================================================================
-- 数据库增量升级脚本: 20260917_anchors_anchor_type.sql
-- 针对 anchors 表增加 anchor_type 主播类型字段 (带货/娱乐/专家/闲聊)
-- ============================================================================

ALTER TABLE anchors ADD COLUMN anchor_type VARCHAR(32) DEFAULT 'ecommerce';

-- 针对存量历史主播数据，如为空则兜底填充为带货主播
UPDATE anchors SET anchor_type = 'ecommerce' WHERE anchor_type IS NULL OR anchor_type = '';
