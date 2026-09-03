-- MEME-05: 四阶段生命周期表
CREATE TABLE IF NOT EXISTS biz.asset_lifecycle (
    asset_id          BIGINT PRIMARY KEY REFERENCES core.asset(asset_id),
    stage             VARCHAR(16) NOT NULL,   -- launch/bloom/diverge/decay/unknown
    age_days          INTEGER,
    liquidity_usd     NUMERIC,
    holder_change_30d NUMERIC,
    social_score      NUMERIC,
    proxy_used        BOOLEAN DEFAULT FALSE,
    computed_at       TIMESTAMP DEFAULT NOW(),
    detail            JSONB
);
