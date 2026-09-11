-- 赛道 TVL 日频快照（DeFi Llama）
-- 来源：DeFi Llama /protocols 全量协议按 category 聚合
-- 用途：叙事榜 TVL 腿、赛道资金流判断

CREATE TABLE IF NOT EXISTS biz.category_tvl_daily (
    snapshot_date      DATE           NOT NULL,
    category           VARCHAR(100)   NOT NULL,
    tvl_usd            NUMERIC(24,2)  NOT NULL DEFAULT 0,
    tvl_change_7d_pct  NUMERIC(12,4),
    protocol_count     INT            NOT NULL DEFAULT 0,
    source_code        VARCHAR(20)    NOT NULL DEFAULT 'defillama',
    fetched_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (snapshot_date, category)
);

CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_date ON biz.category_tvl_daily(snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_cat ON biz.category_tvl_daily(category);

COMMENT ON TABLE biz.category_tvl_daily IS '赛道 TVL 日频快照（DeFi Llama /protocols 按 category 聚合）';
