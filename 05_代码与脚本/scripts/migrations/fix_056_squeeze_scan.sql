-- ============================================================
-- fix_056_squeeze_scan.sql
-- 轧空行情扫描（scan/squeeze）数据基础（幂等）
--   1. biz.liquidation_snapshot  CoinGlass coin-list 高频轮询落库（爆仓滚动窗口）
--   2. biz.long_short_ratio      Binance /futures/data/* 多空比（按需采集，5m 粒度）
--   3. biz.squeeze_track         轧空跟踪队列状态（扫描→跟踪→判定）
--   4. biz.scan_signal 增加 detail JSONB（结构化判定明细，含结论/指标/原因）
--
-- 背景：轧空标注只用 Binance 免费端点 + CoinGlass HOBBYIST 可达范围；
--       爆仓无细粒度历史（HOBBYIST 最小 4h），只能靠 coin-list 滚动窗口高频轮询差分。
-- ============================================================

-- 1. 爆仓滚动窗口快照（CoinGlass liquidation/coin-list，全币种一次拉取）
--    注意：各列是「滚动窗口累计值」（近 1h/4h/12h/24h 爆仓额）。
--    ⚠️ 消费侧必须取**绝对值**（＝最近 1h 爆仓额），严禁跨桶差分：相邻两次
--    滚动快照相减得到的是「新滚入 − 滚出」，平稳时≈0、回落时常为负，
--    再 max(…,0) 截断即假 0（2026-09-21 审计 P1-1）。
CREATE TABLE IF NOT EXISTS biz.liquidation_snapshot (
    symbol            TEXT         NOT NULL,        -- 合约符号（与本库资产口径一致，如 BTCUSDT）
    ts                TIMESTAMPTZ  NOT NULL,        -- 采样时间（对齐采样边界）
    source            TEXT         NOT NULL DEFAULT 'coinglass',
    liq_usd_1h        NUMERIC(24,2),
    liq_usd_4h        NUMERIC(24,2),
    liq_usd_12h       NUMERIC(24,2),
    liq_usd_24h       NUMERIC(24,2),
    long_liq_usd_1h   NUMERIC(24,2),                -- 多单爆仓（价格下跌被强平）
    short_liq_usd_1h  NUMERIC(24,2),                -- 空单爆仓（价格上涨被强平）
    long_liq_usd_4h   NUMERIC(24,2),
    short_liq_usd_4h  NUMERIC(24,2),
    fetched_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_snapshot_ts ON biz.liquidation_snapshot (ts DESC);
CREATE INDEX IF NOT EXISTS idx_liq_snapshot_sym_ts ON biz.liquidation_snapshot (symbol, ts DESC);

COMMENT ON TABLE biz.liquidation_snapshot IS
    'CoinGlass coin-list 滚动爆仓窗口快照（多空分列）；轧空扫描取绝对值（最近 1h 爆仓额），不跨桶差分';

-- 2. 多空比（Binance 免费 /futures/data/*，仅对入队/候选币按需采集）
CREATE TABLE IF NOT EXISTS biz.long_short_ratio (
    symbol              TEXT         NOT NULL,      -- 合约符号，如 BTCUSDT
    period              TEXT         NOT NULL DEFAULT '5m',
    ts                  TIMESTAMPTZ  NOT NULL,      -- 数据点时间（Binance timestamp）
    top_position_ratio  NUMERIC(12,6),              -- 大户持仓量多空比
    top_account_ratio   NUMERIC(12,6),              -- 大户账户数多空比
    global_ratio        NUMERIC(12,6),              -- 全体账户多空比
    taker_ratio         NUMERIC(12,6),              -- 主动买卖量比（>1 主动买占优）
    fetched_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, period, ts)
);
CREATE INDEX IF NOT EXISTS idx_long_short_ratio_sym_ts ON biz.long_short_ratio (symbol, ts DESC);

COMMENT ON TABLE biz.long_short_ratio IS
    'Binance /futures/data/* 多空比（大户持仓/大户账户/全体账户/主动买卖），5m 粒度，按需采集';

-- 3. 轧空跟踪队列（一币一行，status='tracking' 时唯一）
CREATE TABLE IF NOT EXISTS biz.squeeze_track (
    id               BIGSERIAL    PRIMARY KEY,
    symbol           TEXT         NOT NULL,
    status           TEXT         NOT NULL DEFAULT 'tracking',  -- tracking / judged / expired
    started_at       TIMESTAMPTZ  NOT NULL,                     -- 入队时间
    surge_start_ts   TIMESTAMPTZ,                              -- 拉升起点（窗口起点）
    surge_start_px   NUMERIC(24,8),                            -- 拉升起点价
    surge_pct        NUMERIC(8,2),                             -- 入队时涨幅 %
    peak_px          NUMERIC(24,8),                            -- 跟踪期最高价
    peak_ts          TIMESTAMPTZ,                              -- 最高价出现时间
    last_px          NUMERIC(24,8),
    last_ts          TIMESTAMPTZ,
    retrace_pct      NUMERIC(8,2),                             -- 距高点回撤 %
    conclusion       TEXT,                                     -- long_win/short_win/profit_take/churn
    reason           TEXT,
    metrics          JSONB,                                    -- 判定窗口指标快照
    judged_at        TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_squeeze_track_active
    ON biz.squeeze_track (symbol) WHERE status = 'tracking';
CREATE INDEX IF NOT EXISTS idx_squeeze_track_status
    ON biz.squeeze_track (status, started_at DESC);

COMMENT ON TABLE biz.squeeze_track IS
    '轧空行情跟踪队列：命中疑似轧空入队(tracking)，冲高回撤后判定(judged)，超时(expired)';

-- 4. scan_signal 结构化明细（轧空判定结论/指标/原因；其他池可空）
ALTER TABLE biz.scan_signal
    ADD COLUMN IF NOT EXISTS detail JSONB;

COMMENT ON COLUMN biz.scan_signal.detail IS
    '结构化明细（轧空判定: conclusion/reason/窗口指标；主池/蓄势池可空）';