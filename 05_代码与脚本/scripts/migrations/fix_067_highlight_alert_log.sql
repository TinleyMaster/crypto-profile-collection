-- ============================================================
-- fix_067_highlight_alert_log.sql
-- 高亮信号 · 增量邮件提醒去重表（幂等）
--   1. biz.highlight_alert_log  高亮卡片发送记录 + 原子发送锁
-- 设计依据：04_架构与代码方案/高亮信号代码逻辑_2026-09-23.md §11
-- 调用方：scripts/bin/send_highlight_alert.py（每小时探测，仅新增/升级发信）
-- 范式同 biz.catalyst_notification_log（UNIQUE + INSERT ON CONFLICT 原子加锁）
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.highlight_alert_log (
    log_id              BIGSERIAL PRIMARY KEY,
    -- 卡片指纹 = lower(target) || '|' || 主 signal_type
    -- 高亮信号无独立主键（派生自 overview 快照），故用指纹作去重键
    card_key            TEXT NOT NULL,
    -- 提醒类型：new（首次出现）/ upgrade（tier 升级 或 共振源数增加）
    alert_kind          VARCHAR(16) NOT NULL,
    target              TEXT,
    primary_signal_type TEXT,
    tier                VARCHAR(4),            -- HIGH / MED
    score               NUMERIC(6,1),          -- 发送时刻 conviction_score
    resonance_count     INT,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    subject             VARCHAR(256),
    status              VARCHAR(16) NOT NULL DEFAULT 'sent',  -- sending / sent / failed
    error_msg           TEXT,
    -- 同一卡片的同类提醒只保留一行，靠 sent_at 做冷却窗口判定
    UNIQUE (card_key, alert_kind)
);

-- 冷却窗口查询：按类型 + 时间倒序（与 catalyst_notification_log 同构）
CREATE INDEX IF NOT EXISTS idx_hl_alert_kind_time
    ON biz.highlight_alert_log (alert_kind, sent_at DESC);

-- 卡片历史状态查询：按指纹取最近一条已发送记录
CREATE INDEX IF NOT EXISTS idx_hl_alert_card_time
    ON biz.highlight_alert_log (card_key, sent_at DESC);

COMMENT ON TABLE  biz.highlight_alert_log IS '高亮信号增量邮件提醒去重/发送日志（send_highlight_alert.py）';
COMMENT ON COLUMN biz.highlight_alert_log.card_key IS '卡片指纹 lower(target)|primary_signal_type；高亮卡片无主键，用指纹去重';
COMMENT ON COLUMN biz.highlight_alert_log.alert_kind IS 'new=首次出现；upgrade=tier 升到 HIGH 或共振源数增加';