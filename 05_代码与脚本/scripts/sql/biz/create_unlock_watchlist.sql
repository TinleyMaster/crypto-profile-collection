-- 解锁追踪列表（watchlist）：跟踪代币大额解锁，做空/观察提醒
CREATE TABLE IF NOT EXISTS biz.unlock_watchlist (
    watch_id            SERIAL PRIMARY KEY,
    asset_id            INTEGER NOT NULL,
    symbol              TEXT,
    short_plan_note     TEXT,               -- 做空计划备注
    target_unlock_date  DATE,               -- 目标解锁日期
    target_unlock_pct   NUMERIC(8,2),       -- 目标解锁占比（%）
    entry_price         NUMERIC(24,8),      -- 加入追踪时的价格
    last_price          NUMERIC(24,8),      -- 最新价格
    last_price_at       TIMESTAMPTZ,        -- 最新价格获取时间
    unlock_alert_sent_at TIMESTAMPTZ,       -- 解锁提醒已发送时间
    trend_alert_sent_at  TIMESTAMPTZ,       -- 空头趋势提醒已发送时间
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_watchlist_asset UNIQUE (asset_id),
    CONSTRAINT fk_watchlist_asset
        FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE biz.unlock_watchlist IS
    '解锁追踪列表：跟踪代币大额解锁事件，记录做空计划、目标解锁日期与价格，用于到期提醒与空头趋势提醒。';

CREATE INDEX IF NOT EXISTS idx_watchlist_unlock_date
    ON biz.unlock_watchlist (target_unlock_date);
