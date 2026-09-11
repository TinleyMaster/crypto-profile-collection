-- BTC 未平仓合约（OI）日频数据
-- 来源：Binance Futures openInterestHist API（1d period）
-- 用途：价格-OI 背离检测、衍生品热度判断

CREATE TABLE IF NOT EXISTS biz.btc_oi_daily (
    metric_date    DATE           NOT NULL PRIMARY KEY,
    open_interest  NUMERIC(24,4) NOT NULL,
    source_code    VARCHAR(20)    NOT NULL DEFAULT 'binance',
    fetched_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_btc_oi_daily_date ON biz.btc_oi_daily(metric_date DESC);

COMMENT ON TABLE biz.btc_oi_daily IS 'BTC 期货未平仓合约日频数据（Binance），单位：USDT 等值';
