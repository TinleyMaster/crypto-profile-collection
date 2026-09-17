-- ============================================================
-- fix_044_scan_alert.sql
-- 盘面异动扫描 · 双层告警数据基础（幂等）
--   1. biz.event_watchlist   事件预置层（解锁/链上大额转账先行观察名单）
--   2. biz.scan_signal 增加 alerted_at 列（盘面触发层告警去重/冷却）
-- ============================================================

-- 1. 事件预置观察名单（每个 symbol+event_type 保留最新一条）
CREATE TABLE IF NOT EXISTS biz.event_watchlist (
    id            BIGSERIAL PRIMARY KEY,
    asset_id      BIGINT,
    symbol        TEXT           NOT NULL,
    event_type    TEXT           NOT NULL,     -- unlock / onchain_transfer
    event_date    DATE,                        -- unlock 目标日期（onchain 为 NULL）
    event_pct     NUMERIC(8,2),                -- unlock 占比 %
    detail        TEXT,                        -- 描述（如最近大额转账摘要）
    source_ref    JSONB,                       -- 来源明细（如最新转账记录）
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_watchlist_sym_type
    ON biz.event_watchlist (symbol, event_type);
CREATE INDEX IF NOT EXISTS idx_event_watchlist_type_date
    ON biz.event_watchlist (event_type, event_date);

COMMENT ON TABLE biz.event_watchlist IS
    '盘面异动扫描·事件预置层：解锁/链上大额转账等领先型事件观察名单（事件先行→盘面触发碰撞）';

-- 2. scan_signal 告警标记（盘面触发层去重/冷却：同币 12h 内不重复告警）
ALTER TABLE biz.scan_signal
    ADD COLUMN IF NOT EXISTS alerted_at TIMESTAMPTZ;

COMMENT ON COLUMN biz.scan_signal.alerted_at IS '已发送实时告警的时间（NULL=未告警）';
