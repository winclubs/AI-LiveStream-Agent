-- ============================================================================
-- 数据库增量升级脚本: 20260917_voice_profiles_provider.sql
-- 针对 voice_profiles 音色资产库增加所属语音引擎与音色分类字段
-- ============================================================================

ALTER TABLE voice_profiles ADD COLUMN provider_name VARCHAR(64) DEFAULT '';
ALTER TABLE voice_profiles ADD COLUMN voice_type VARCHAR(32) DEFAULT 'preset';

-- 历史数据补齐：针对已有克隆音色设置类型与引擎
UPDATE voice_profiles SET voice_type = 'cloned', provider_name = 'cosyvoice' 
WHERE id NOT LIKE 'zh-%' AND id != 'voice_default_female';

UPDATE voice_profiles SET voice_type = 'preset', provider_name = 'edge_tts' 
WHERE id LIKE 'zh-%';
