-- ============================================================
-- fix_045_funding_hist.sql
-- 盘面异动扫描 · funding 历史序列（幂等）
--   资金费率 8h 结算，REST /fapi/v1/fundingRate 免费提供约 333 天历史，
--   用于回测的 funding 消融（设计方案 §8 局限项 4）。
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.funding_rate_hist (
    symbol        TEXT           NOT NULL,
    funding_time  TIMESTAMPTZ    NOT NULL,     -- 结算时间
    rate          NUMERIC(12,8),               -- 结算费率（小数）
    source_code   VARCHAR(20)    NOT NULL DEFAULT 'binance',
    fetched_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, funding_time)
);
CREATE INDEX IF NOT EXISTS idx_funding_rate_hist_time
    ON biz.funding_rate_hist (funding_time DESC);

COMMENT ON TABLE biz.funding_rate_hist IS
    '资金费率历史序列（Binance 8h 结算），供回测 funding 消融与实时拥挤度标签';
