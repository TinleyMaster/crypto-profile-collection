-- fix_026: Meme 五维风险标签表
-- 对应工单 MEME-04（合约安全/流动性/筹码集中度/生命周期/社交热度 → 风险标签）

BEGIN;

CREATE TABLE IF NOT EXISTS biz.asset_risk_labels (
    asset_id        BIGINT PRIMARY KEY REFERENCES core.asset(asset_id),
    contract_score  NUMERIC(5,2),
    contract_label  VARCHAR(8),          -- red/yellow/green/unknown
    liquidity_score NUMERIC(5,2),
    liquidity_label VARCHAR(8),
    holder_score    NUMERIC(5,2),
    holder_label    VARCHAR(8),
    lifecycle_score NUMERIC(5,2),
    lifecycle_label VARCHAR(8),
    social_score    NUMERIC(5,2),
    social_label    VARCHAR(8),
    axes_computed   INTEGER,             -- 1-5，已算轴数
    total_score     NUMERIC(5,2),        -- 已知轴加权重归一
    risk_label      VARCHAR(16),         -- block/high/medium/low/unknown
    flags           TEXT[],              -- 一票否决/红旗明细
    detail          JSONB,               -- 每轴取数来源与命中规则
    computed_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_risk_labels_label ON biz.asset_risk_labels (risk_label);
CREATE INDEX IF NOT EXISTS idx_asset_risk_labels_score ON biz.asset_risk_labels (total_score DESC);

COMMIT;
