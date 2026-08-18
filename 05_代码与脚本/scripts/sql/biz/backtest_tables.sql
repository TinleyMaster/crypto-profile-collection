-- 每日推荐存档表：用于回测推荐质量
CREATE TABLE IF NOT EXISTS biz.daily_recommendation (
    rec_date        DATE NOT NULL,
    rank            INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    name            TEXT,
    chain           TEXT,
    contract        TEXT,
    sector          TEXT,
    source_count    INTEGER,
    composite_score NUMERIC(6,2),
    change_24h      NUMERIC(8,2),
    volume_24h      NUMERIC(20,2),
    price_usd       NUMERIC(18,8),
    market_cap_usd  NUMERIC(20,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rec_date, symbol, chain)
);

CREATE INDEX IF NOT EXISTS idx_daily_rec_date ON biz.daily_recommendation (rec_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_rec_symbol ON biz.daily_recommendation (symbol, rec_date);

COMMENT ON TABLE biz.daily_recommendation IS '每日投研推荐存档，用于回测推荐命中率和收益表现';

-- 每日价格快照表：用于回测（从 CG/CMC 等已有数据源同步）
CREATE TABLE IF NOT EXISTS biz.asset_price_daily (
    asset_id        INTEGER REFERENCES core.asset(asset_id),
    price_date      DATE NOT NULL,
    price_usd       NUMERIC(18,8),
    market_cap_usd  NUMERIC(20,2),
    volume_24h_usd  NUMERIC(20,2),
    change_24h_pct  NUMERIC(8,2),
    source          TEXT NOT NULL DEFAULT 'cg',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, price_date)
);

CREATE INDEX IF NOT EXISTS idx_asset_price_date ON biz.asset_price_daily (price_date DESC);

COMMENT ON TABLE biz.asset_price_daily IS '每日价格快照，用于回测和趋势分析';
