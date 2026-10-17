-- 数据库升级脚本：商业化直播运行档位、状态细节与平台确认列
-- 对应迁移版本：0014_commercial_session_modes_and_states
-- 制定日期：2026-10-11

-- 1. 增加运行档位列 (DEMO / PREVIEW / PILOT / PRODUCTION)
ALTER TABLE live_session_records ADD COLUMN execution_mode VARCHAR(32) DEFAULT 'PREVIEW';

-- 2. 增加细分状态机详情列 (PRECHECKING / LOCAL_READY / OBS_STREAMING / PLATFORM_CONFIRMED / AI_RUNNING 等)
ALTER TABLE live_session_records ADD COLUMN state_detail VARCHAR(64) DEFAULT 'LOCAL_READY';

-- 3. 增加平台是否已实际确认开播列 (0=未确认, 1=已确认)
ALTER TABLE live_session_records ADD COLUMN platform_confirmed INTEGER DEFAULT 0;

-- 4. 增加平台双向确认时间戳
ALTER TABLE live_session_records ADD COLUMN platform_confirmed_at DATETIME;
