-- DEX 流动性聚合表（MEME-03）
-- 按资产聚合跨链跨池流动性，供五维风险评分消费
CREATE TABLE IF NOT EXISTS biz.asset_liquidity (
    asset_id             BIGINT PRIMARY KEY REFERENCES core.asset(asset_id),
    chain                VARCHAR(32),
    pool_count           INTEGER,
    total_liquidity_usd  NUMERIC(24,2),
    top_pool_share_pct   NUMERIC(6,2),
    cex_listed           BOOLEAN,
    cex_exchanges        TEXT[],
    source               VARCHAR(32),
    source_status        VARCHAR(16),   -- hit / not_cached / error / na
    raw_json             JSONB,
    scanned_at           TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_liquidity_chain
    ON biz.asset_liquidity (chain);
