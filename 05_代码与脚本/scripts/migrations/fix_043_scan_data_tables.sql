-- ============================================================
-- fix_043_scan_data_tables.sql
-- 盘面异动扫描系统 P0 数据基础：5 张新表（幂等）
--   1. biz.asset_klines          分钟级 K 线（L1 粗筛 + 回测数据源）
--   2. biz.oi_cvd_snapshot       OI/CVD 采样历史序列（蓄势池判定前提）
--   3. biz.scan_sampler_state    OI/CVD 采样器增量游标（CVD 增量计算）
--   4. biz.liquidation_events    清算事件（实时订阅落库）
--   5. biz.scan_signal           扫描信号输出（主池/蓄势池统一出口）
-- ============================================================

-- 1. 分钟级 K 线（Binance USDT 永续，5m/15m/1h）
CREATE TABLE IF NOT EXISTS biz.asset_klines (
    symbol        TEXT           NOT NULL,          -- 合约符号，如 BTCUSDT
    interval      TEXT           NOT NULL,          -- 5m / 15m / 1h
    open_time     TIMESTAMPTZ    NOT NULL,          -- K 线开盘时间
    open_px       NUMERIC(24,8),
    high_px       NUMERIC(24,8),
    low_px        NUMERIC(24,8),
    close_px      NUMERIC(24,8),
    base_vol      NUMERIC(30,8),                    -- 成交量（币）
    quote_vol     NUMERIC(30,2),                    -- 成交额（USDT）
    trade_count   BIGINT,
    fetched_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_asset_klines_symbol_interval_ot
    ON biz.asset_klines (symbol, interval, open_time DESC);
CREATE INDEX IF NOT EXISTS idx_asset_klines_interval_ot
    ON biz.asset_klines (interval, open_time DESC);

COMMENT ON TABLE biz.asset_klines IS 'Binance USDT 永续分钟级 K 线（5m/15m/1h），供 L1 粗筛与回测';

-- 2. OI/CVD 采样历史序列（5 分钟粒度，蓄势池"OI 持续抬升"判定前提）
CREATE TABLE IF NOT EXISTS biz.oi_cvd_snapshot (
    symbol       TEXT           NOT NULL,
    ts           TIMESTAMPTZ    NOT NULL,           -- 采样时间（对齐 5m 边界）
    exchange     TEXT           NOT NULL DEFAULT 'binance',
    oi_usd       NUMERIC(24,2),                     -- OI 总价值（USDT）
    cvd_5m_usd   NUMERIC(24,2),                     -- 本采样窗口净主动成交（正=主动买盘强）
    cvd_1h_usd   NUMERIC(24,2),                     -- 近 1h 累计 CVD（写后回填）
    vol_5m_usd   NUMERIC(24,2),                     -- 本采样窗口总成交额
    PRIMARY KEY (symbol, exchange, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_cvd_snapshot_ts ON biz.oi_cvd_snapshot (ts DESC);
CREATE INDEX IF NOT EXISTS idx_oi_cvd_snapshot_sym_ts ON biz.oi_cvd_snapshot (symbol, ts DESC);

COMMENT ON TABLE biz.oi_cvd_snapshot IS 'OI/CVD 5 分钟采样历史序列（Binance），供 OI↑↓、蓄势判定、回测';

-- 3. 采样器增量游标（CVD 增量计算：记录每个币已处理到的 aggTrades id）
CREATE TABLE IF NOT EXISTS biz.scan_sampler_state (
    symbol         TEXT       PRIMARY KEY,
    last_trade_id  BIGINT     NOT NULL DEFAULT 0,   -- 已处理的最大 aggTrade id
    last_oi_usd    NUMERIC(24,2),                   -- 上一次采样 OI（备用）
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.scan_sampler_state IS 'OI/CVD 采样器增量游标，记录每币已处理的最大 aggTrade id';

-- 4. 清算事件（Binance WebSocket !forceOrder@arr 实时落库）
CREATE TABLE IF NOT EXISTS biz.liquidation_events (
    id           BIGSERIAL PRIMARY KEY,
    event_ts     TIMESTAMPTZ    NOT NULL,           -- 清算成交时间
    symbol       TEXT           NOT NULL,
    side         TEXT,                              -- SELL/BUY（清算单方向）
    qty          NUMERIC(24,8),
    price        NUMERIC(24,8),
    usd_value    NUMERIC(24,2),                     -- 清算名义价值
    order_id     BIGINT,                            -- 原始订单 id（去重用）
    exchange     TEXT           NOT NULL DEFAULT 'binance',
    ingested_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_liq_events_sym_ts ON biz.liquidation_events (symbol, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_liq_events_ts ON biz.liquidation_events (event_ts DESC);

COMMENT ON TABLE biz.liquidation_events IS 'Binance 全市场清算事件流落库，供诱多/诱空场景辅助验证';

-- 5. 扫描信号输出（主池 + 蓄势池统一出口，供观察清单与回测消费）
CREATE TABLE IF NOT EXISTS biz.scan_signal (
    id            BIGSERIAL PRIMARY KEY,
    signal_ts     TIMESTAMPTZ    NOT NULL,
    symbol        TEXT           NOT NULL,
    pool          TEXT           NOT NULL,          -- 'main' / 'accumulation'
    scenario      TEXT,                             -- S1..S8 / ACC / BRK
    timeframe     TEXT,                             -- 5m/15m/1h
    p_dir         TEXT,
    price_chg_pct NUMERIC(8,2),
    vol_state     TEXT,
    vol_ratio     NUMERIC(8,2),
    oi_dir        TEXT,
    oi_chg_pct    NUMERIC(8,2),
    cvd_dir       TEXT,
    funding_rate  NUMERIC(12,8),
    confidence    TEXT,                             -- high/medium/low
    context_tags  TEXT[],                           -- 市场环境标签（regime）
    trigger_price NUMERIC(24,8),
    stop_loss_pct NUMERIC(8,2),
    status        TEXT NOT NULL DEFAULT 'active',   -- active/confirmed/stale/closed
    expired_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scan_signal_ts ON biz.scan_signal (signal_ts DESC);
CREATE INDEX IF NOT EXISTS idx_scan_signal_sym ON biz.scan_signal (symbol, signal_ts DESC);
CREATE INDEX IF NOT EXISTS idx_scan_signal_status ON biz.scan_signal (status, signal_ts DESC);

COMMENT ON TABLE biz.scan_signal IS '盘面异动扫描信号输出（主池/蓄势池统一出口）';
