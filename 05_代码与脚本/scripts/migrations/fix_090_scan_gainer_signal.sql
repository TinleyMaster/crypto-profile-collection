-- v2 盘面异动扫描：涨幅榜定位信号表（盘面异动扫描系统设计方案 v2）
-- 信号类型：
--   SHORT_LONG  短线做多（chg24h≥50% + 放量）    窗口 T+24h
--   MID_LONG    次档做多（chg24h 20~50% + 放量）  窗口 T+24h
--   TRAP_SHORT  诱多做空（chg24h<5% + 放量）      窗口 T+24h
--   EXT_SHORT   极端做空（chg24h≥75% + 低量静默） 窗口 T+7d
CREATE TABLE IF NOT EXISTS biz.scan_gainer_signal (
    id BIGSERIAL PRIMARY KEY,
    scan_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol TEXT NOT NULL,
    price_usd NUMERIC(24,10),
    chg_24h_pct NUMERIC(10,4),
    vol_24h_usd NUMERIC(30,2),
    vol_ratio_7d NUMERIC(10,3),
    funding_rate NUMERIC(12,8),
    signal_type TEXT NOT NULL,
    signal_window TEXT NOT NULL,          -- T24H / T7D
    direction TEXT NOT NULL,              -- LONG / SHORT
    params JSONB,                         -- 最优交易参数快照（N/TP/SL/Lev）
    status TEXT NOT NULL DEFAULT 'active',-- active / ordered / skipped / expired
    exec_state TEXT,
    executed_at TIMESTAMPTZ,
    alerted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_scan_gainer_signal_ts ON biz.scan_gainer_signal (scan_ts DESC);
CREATE INDEX IF NOT EXISTS idx_scan_gainer_signal_sym ON biz.scan_gainer_signal (symbol, status);
