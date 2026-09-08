-- 加密货币 ETF 日频资金流表（长表）
-- 数据源：cryptoetf.today API（/v1/flows/{asset}）
-- 覆盖 13 种资产：BTC, ETH, SOL, XRP, HYPE, DOGE, LINK, AVAX, HBAR, LTC, BNB, DOT, SUI

CREATE TABLE IF NOT EXISTS biz.etf_flow_daily (
    symbol            TEXT        NOT NULL,   -- 币种代码（BTC/ETH/SOL/...），大写
    flow_date         DATE        NOT NULL,   -- 资金流日期（交易日）
    net_flow_usd      NUMERIC(20,2),          -- 当日净流入金额（USD），正=流入，负=流出
    net_flow_usd_m    NUMERIC(12,2),          -- 当日净流入（百万 USD），方便直接展示
    aum_usd           NUMERIC(20,2),          -- 当日 AUM（USD），如 API 提供
    total_inflow_usd  NUMERIC(20,2),          -- 当日总申购额（USD），如 API 提供
    total_outflow_usd NUMERIC(20,2),          -- 当日总赎回额（USD），如 API 提供
    source_code       TEXT        NOT NULL DEFAULT 'cryptoetf',  -- 数据源编码
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),        -- 本次采集时间
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),        -- 最后更新时间
    PRIMARY KEY (symbol, flow_date, source_code)
);

CREATE INDEX IF NOT EXISTS ix_etf_flow_daily_date
    ON biz.etf_flow_daily (flow_date DESC);

CREATE INDEX IF NOT EXISTS ix_etf_flow_daily_symbol_date
    ON biz.etf_flow_daily (symbol, flow_date DESC);

COMMENT ON TABLE biz.etf_flow_daily IS '加密货币 ETF 日频资金流（cryptoetf.today API），13 种资产日频净流入/流出';
COMMENT ON COLUMN biz.etf_flow_daily.net_flow_usd IS '当日净流入金额（USD），正=机构申购净流入，负=机构赎回净流出';
COMMENT ON COLUMN biz.etf_flow_daily.source_code IS '数据源：cryptoetf = cryptoetf.today API';
