-- 恐贪指数日频数据
-- 来源：CoinMarketCap fear-and-greed API（alternative.me 同源）
-- 用途：大盘情绪维度计算、历史分位、极值判断

CREATE TABLE IF NOT EXISTS biz.fear_greed_daily (
    metric_date    DATE        NOT NULL PRIMARY KEY,
    value          INT         NOT NULL,                   -- 0-100
    value_class    VARCHAR(20),                            -- Extreme Fear / Fear / Neutral / Greed / Extreme Greed
    source_code    VARCHAR(20) NOT NULL DEFAULT 'cmc',
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_date ON biz.fear_greed_daily(metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_value ON biz.fear_greed_daily(value);

COMMENT ON TABLE biz.fear_greed_daily IS '恐贪指数日频数据（CMC/alternative.me），0=极度恐惧，100=极度贪婪';
