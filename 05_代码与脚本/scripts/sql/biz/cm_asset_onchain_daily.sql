-- Coin Metrics Community 档链上日频指标表（仅达标主流币）
-- 数据源：github.com/coinmetrics/data（CC BY-NC 4.0）
-- 数据冻结于 2026-05-24，纯历史分位用

CREATE TABLE IF NOT EXISTS biz.cm_asset_onchain_daily (
    asset_id                    INTEGER  NOT NULL REFERENCES core.asset(asset_id),
    cm_symbol                   TEXT     NOT NULL,          -- 如 'btc'
    metric_date                 DATE     NOT NULL,
    price_usd                   NUMERIC,
    cap_mvrv_cur                NUMERIC,  -- MVRV 市值（CapMVRVCur）
    adr_act_cnt                 BIGINT,   -- 活跃地址
    tx_tfr_cnt                  BIGINT,   -- 转账笔数
    flow_in_ex_usd              NUMERIC,  -- 交易所净流入 USD
    flow_out_ex_usd             NUMERIC,  -- 交易所净流出 USD
    roi_30d                     NUMERIC,
    roi_1yr                     NUMERIC,
    volume_reported_spot_usd_1d NUMERIC,
    source_cutoff               DATE     NOT NULL DEFAULT '2026-05-24',  -- 数据截止标注
    PRIMARY KEY (asset_id, metric_date)
);

CREATE INDEX IF NOT EXISTS ix_cm_onchain_asset_date ON biz.cm_asset_onchain_daily (asset_id, metric_date);
CREATE INDEX IF NOT EXISTS ix_cm_onchain_symbol ON biz.cm_asset_onchain_daily (cm_symbol);

COMMENT ON TABLE biz.cm_asset_onchain_daily IS 'Coin Metrics Community 档链上日频指标（仅达标主流币）；数据冻结 2026-05-24，纯历史分位用';
COMMENT ON COLUMN biz.cm_asset_onchain_daily.source_cutoff IS '数据源截止日期，所有行均为 2026-05-24，严禁伪装实时';
