-- DEX 流动性聚合表（MEME-03）：按资产×链每链一行，供五维风险评分按 asset_id 聚合消费
CREATE TABLE IF NOT EXISTS biz.asset_liquidity (
    asset_id             BIGINT NOT NULL REFERENCES core.asset(asset_id),
    chain                VARCHAR(32) NOT NULL,
    pool_count           INTEGER,
    total_liquidity_usd  NUMERIC(24,2),
    top_pool_share_pct   NUMERIC(6,2),
    cex_listed           BOOLEAN,
    cex_exchanges        TEXT[],
    source               VARCHAR(32),
    source_status        VARCHAR(16),   -- hit / not_cached / error / na
    raw_json             JSONB,
    scanned_at           TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (asset_id, chain)
);

CREATE INDEX IF NOT EXISTS idx_asset_liquidity_chain
    ON biz.asset_liquidity (chain);
