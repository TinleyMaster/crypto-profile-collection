-- 稳定币总供给日频表（DeFi Llama stablecoins API）
-- 数据源：https://stablecoins.llama.fi/stablecoincharts/All
-- 用途：计算稳定币净流入、7d滚动累计、分位等大盘指标

CREATE TABLE IF NOT EXISTS biz.stablecoin_supply_daily (
    metric_date       DATE        NOT NULL,   -- 数据日期
    total_supply_usd  NUMERIC(24,2),          -- 全市场稳定币总流通供给（USD）
    net_flow_usd      NUMERIC(20,2),          -- 当日净流入 = 当日供给 - 前日供给
    source_code       TEXT        NOT NULL DEFAULT 'defillama',  -- 数据源编码
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),        -- 本次采集时间
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),        -- 最后更新时间
    PRIMARY KEY (metric_date, source_code)
);

CREATE INDEX IF NOT EXISTS ix_stablecoin_supply_daily_date
    ON biz.stablecoin_supply_daily (metric_date DESC);

COMMENT ON TABLE biz.stablecoin_supply_daily IS '稳定币总供给日频数据（DeFi Llama），用于计算净流入/滚动/分位';
COMMENT ON COLUMN biz.stablecoin_supply_daily.total_supply_usd IS '全市场稳定币总流通供给（peggedUSD）';
COMMENT ON COLUMN biz.stablecoin_supply_daily.net_flow_usd IS '当日净流入 = 当日供给 - 前日供给，正=流入，负=流出';
COMMENT ON COLUMN biz.stablecoin_supply_daily.source_code IS '数据源：defillama = DeFi Llama stablecoins API';
