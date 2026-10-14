-- fix_097: 基本面因子层（Alpha 挖掘的数据底座）
-- 从各原始表派生数值因子 → biz.asset_factor_daily（asset_id + factor_date + factor_code 主键）
-- 幂等，可重复执行

BEGIN;

CREATE TABLE IF NOT EXISTS biz.asset_factor_daily (
    factor_date   DATE   NOT NULL,                 -- 因子生效日（取数据自身快照日，非运行日）
    asset_id      BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    factor_code   VARCHAR(64) NOT NULL,            -- 因子代码，如 holder_top10_pct
    factor_value  NUMERIC(24,6),                   -- 数值因子（布尔因子用 0/1）
    factor_source VARCHAR(32),                     -- 来源表，如 onchain_holder_snapshot
    metadata      JSONB,                           -- 附加口径/说明
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, factor_date, factor_code)
);

COMMENT ON TABLE biz.asset_factor_daily IS
    '基本面因子日表：因子 → 资产 × 日期 × 代码，供 Alpha 挖掘 SOP 直接 join 行情回测';
COMMENT ON COLUMN biz.asset_factor_daily.factor_value IS '数值因子；布尔因子 0/1；缺失为 NULL 不写行';

CREATE INDEX IF NOT EXISTS idx_factor_daily_asset_date
    ON biz.asset_factor_daily (asset_id, factor_date DESC);
CREATE INDEX IF NOT EXISTS idx_factor_daily_code_date
    ON biz.asset_factor_daily (factor_code, factor_date DESC);

COMMIT;
