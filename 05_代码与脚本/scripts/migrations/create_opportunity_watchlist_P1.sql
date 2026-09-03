-- P1：机会观察列表表
-- 用途：用户从机会卡点击"加入观察"后记录资产，供调度监控 conviction 升级/触发告警
-- 风险：写 prod 库 biz.opportunity_watchlist，轻量单列
-- 执行方式：psql $DATABASE_URL -f 05_代码与脚本/scripts/migrations/create_opportunity_watchlist_P1.sql

CREATE TABLE IF NOT EXISTS biz.opportunity_watchlist (
    watch_id SERIAL PRIMARY KEY,
    asset_id INTEGER NOT NULL,
    symbol TEXT,
    trigger_rule TEXT DEFAULT 'conviction_upgrade',  -- 当前默认规则：conviction 从 LOW/MED 升级为 MED/HIGH
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_alerted_at TIMESTAMPTZ,
    alert_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (asset_id)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_watchlist_asset_id ON biz.opportunity_watchlist(asset_id);
CREATE INDEX IF NOT EXISTS idx_opportunity_watchlist_added_at ON biz.opportunity_watchlist(added_at DESC);

-- 验证：\d biz.opportunity_watchlist
-- SELECT COUNT(*) FROM biz.opportunity_watchlist;
