-- Coin Metrics Community 档链上日频指标表（仅达标主流币）
-- 数据源：github.com/coinmetrics/data（CC BY-NC 4.0）
-- 由 CM Community 载入脚本持续追加/刷新（2026-10-01 实测 max(metric_date)=2026-09-29，
-- 近 7 天仍有 83 行入库），纯历史分位用。早前「数据冻结于 2026-05-24」的说法与实况矛盾，已更正。

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

COMMENT ON TABLE biz.cm_asset_onchain_daily IS 'Coin Metrics Community 档链上日频指标（仅达标主流币）；由 CM 载入脚本持续更新，纯历史分位用';
COMMENT ON COLUMN biz.cm_asset_onchain_daily.source_cutoff IS '数据源截止日期，**逐行**标注而非全表统一值（2026-10-01 实测：48,573 行为 2026-09-29，matic 组 2,392 行停留在 2025-11-12）；消费侧须按该列判断新鲜度，严禁伪装实时';
