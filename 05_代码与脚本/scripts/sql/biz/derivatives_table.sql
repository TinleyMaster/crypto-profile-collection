-- 衍生品资金面数据（多交易所聚合）
CREATE TABLE IF NOT EXISTS biz.asset_derivatives (
    asset_id              INTEGER PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    symbol                TEXT NOT NULL,
    -- 资金费率（加权平均，按 OI 加权）
    funding_rate          NUMERIC(12,8),     -- 当前资金费率（小数）
    funding_rate_pct      NUMERIC(8,4),      -- 百分比展示
    next_funding_time     TIMESTAMPTZ,
    funding_rate_7d_avg   NUMERIC(12,8),     -- 7 天平均资金费率
    funding_rate_30d_avg  NUMERIC(12,8),     -- 30 天平均资金费率
    -- 未平仓合约
    total_oi_usd          NUMERIC(20,2),     -- 全市场 OI 总价值（USDT）
    oi_change_24h_pct     NUMERIC(8,2),      -- OI 24h 变化率
    -- CVD（成交净流入）
    cvd_24h_usd           NUMERIC(20,2),     -- 24h 累计 CVD（USDT），正=主动买入净流入
    cvd_ratio_24h         NUMERIC(8,4),      -- CVD / 总成交额
    -- 交易所明细
    exchanges_json        JSONB,             -- 各交易所明细（funding/oi/trades）
    available_exchanges   TEXT[],            -- 有数据的交易所列表
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_derivatives_symbol ON biz.asset_derivatives (symbol);
CREATE INDEX IF NOT EXISTS idx_asset_derivatives_fetched ON biz.asset_derivatives (fetched_at);

COMMENT ON TABLE biz.asset_derivatives IS '衍生品资金面数据（多交易所聚合：Binance/OKX/Bybit/Bitget/Gate）';
