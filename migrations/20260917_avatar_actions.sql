-- ============================================================================
-- 数据库迁移脚本: 20260917_avatar_actions.sql
-- 阶段三：数字人动作切片状态机与带货场景智能绑定
-- ============================================================================

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
    FOREIGN KEY(anchor_id) REFERENCES anchors(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_avatar_actions_anchor_id ON avatar_actions(anchor_id);
CREATE INDEX IF NOT EXISTS ix_avatar_actions_action_code ON avatar_actions(action_code);

-- 插入默认 5 大标准动作配置
INSERT OR IGNORE INTO avatar_actions (id, anchor_id, action_code, action_name, trigger_type, trigger_keywords, trigger_events, duration_sec, priority, mirror_loop, is_active)
VALUES
    ('action_default_0', NULL, 0, '待机呼吸循环', 'manual', '', '', 0.0, 0, 1, 1),
    ('action_default_1', NULL, 1, '热情挥手欢迎', 'both', '欢迎,来了,刚进,哈喽,晚上好', 'welcome', 3.0, 2, 1, 1),
    ('action_default_2', NULL, 2, '求关注与点赞', 'both', '点赞,关注,粉丝团,灯牌,双击', 'follow', 3.5, 3, 1, 1),
    ('action_default_3', NULL, 3, '促单指引购物车', 'both', '购物车,下单,左下角,抢购,手慢无,买一送,拍下', 'order', 4.0, 5, 1, 1),
    ('action_default_4', NULL, 4, '大额打赏致谢', 'both', '感谢,礼物,破费,大气,老板大气', 'gift', 4.5, 9, 1, 1);
