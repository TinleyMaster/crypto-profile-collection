-- 事件类型 × 资产 = 定向市场影响（规则推导，非 LLM）
-- P0-A: 催化剂因子化第一步，把 ai_sentiment 转成对具体资产的定向影响
CREATE TABLE IF NOT EXISTS biz.catalyst_impact (
    impact_id       BIGSERIAL PRIMARY KEY,
    catalyst_id     BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    impact_direction TEXT NOT NULL CHECK (impact_direction IN ('bullish','bearish','neutral')),
    impact_strength TEXT NOT NULL CHECK (impact_strength IN ('strong','medium','weak')),
    horizon_days    INT,
    derived_from    TEXT DEFAULT 'rule',   -- rule | llm
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalyst_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_catalyst_impact_asset
    ON biz.catalyst_impact (asset_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_impact_cat
    ON biz.catalyst_impact (catalyst_id);
