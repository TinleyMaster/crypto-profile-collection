-- ============================================================
-- fix_066_scan_edge_report.sql
-- 告警胜率赔率日报 · 数据基础（幂等）
--   1. biz.scan_signal_outcome   逐信号多窗口结局（T+1h/4h/12h/24h + BTC beta 对照）
--   2. biz.scan_edge_daily       日报聚合 + 阈值-行情失配判定（上海日一行）
--   3. biz.scan_edge_bucket      当日 × 维度 × 桶（分桶诊断）
-- 设计依据：04_架构与代码方案/告警胜率赔率日报方案_2026-09-23.md
-- ============================================================

-- 1. 逐信号多窗口结局（与 biz.catalyst_outcome 同构：多窗口 + 未到期闸门）
CREATE TABLE IF NOT EXISTS biz.scan_signal_outcome (
    signal_id       BIGINT PRIMARY KEY REFERENCES biz.scan_signal(id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,
    pool            TEXT,
    scenario        TEXT,
    timeframe       TEXT,
    p_dir           TEXT,                          -- up / down（方向对齐基准；空 → no_data）
    alerted_at      TIMESTAMPTZ,                   -- 告警时刻（决策起点）
    kline_iv        TEXT,                          -- 取价周期：5m（主）/ 1h（5m 缺失时兜底）
    base_time       TIMESTAMPTZ NOT NULL,          -- 基线 K 线 open_time（<= alerted_at 的最后一根）
    base_px         NUMERIC(24,8),                 -- 基线收盘价

    -- 方向对齐净收益（%，已扣 0.1% 双边费）
    aligned_ret_1h  NUMERIC(10,4),
    aligned_ret_4h  NUMERIC(10,4),
    aligned_ret_12h NUMERIC(10,4),
    aligned_ret_24h NUMERIC(10,4),

    -- BTC 同方向同窗口净收益（beta 对照，%）
    btc_ret_1h      NUMERIC(10,4),
    btc_ret_4h      NUMERIC(10,4),
    btc_ret_12h     NUMERIC(10,4),
    btc_ret_24h     NUMERIC(10,4),

    -- 超额 = aligned_ret - btc_ret（正=信号有 alpha）
    excess_1h       NUMERIC(10,4),
    excess_4h       NUMERIC(10,4),
    excess_12h      NUMERIC(10,4),
    excess_24h      NUMERIC(10,4),

    -- 途中风险（24h 窗口内 1h 高低价，方向对齐）
    mae_24h         NUMERIC(10,4),                 -- 最大不利偏移（%，<=0）
    mfe_24h         NUMERIC(10,4),                 -- 最大有利偏移（%）
    sl_hit_24h      BOOLEAN,                       -- 是否触及 stop_loss_pct（该列为 NULL 时本列 NULL）
    sl_pct_snapshot NUMERIC(8,2),                  -- stop_loss_pct 快照（2026-09-21 08:40 UTC 后才有）

    -- 结算状态
    outcome_state   TEXT NOT NULL DEFAULT 'pending',
                    -- pending(未到 24h) / resolved(已结算 24h) / no_data(无 K 线或 p_dir 空)
    last_window     SMALLINT NOT NULL DEFAULT 0,   -- 已结算的最大窗口小时数（0/1/4/12/24）
    bars_n          SMALLINT,                      -- 24h 窗口内可用 1h K 线数（<24 → partial）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 已建表时补列（本迁移首次执行与重复执行均安全）
ALTER TABLE biz.scan_signal_outcome
    ADD COLUMN IF NOT EXISTS kline_iv TEXT;

CREATE INDEX IF NOT EXISTS idx_scan_outcome_state
    ON biz.scan_signal_outcome (outcome_state, alerted_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_outcome_alerted
    ON biz.scan_signal_outcome (alerted_at DESC);

COMMENT ON TABLE biz.scan_signal_outcome IS
    '盘面告警逐信号多窗口结局（方向对齐净收益 + BTC beta 对照），供胜率赔率日报消费';
COMMENT ON COLUMN biz.scan_signal_outcome.last_window IS
    '已结算的最大窗口小时数；未到期行不得混入日报统计（同 catalyst_outcome 教训）';

-- 2. 日报聚合（上海日一行）
CREATE TABLE IF NOT EXISTS biz.scan_edge_daily (
    report_date       DATE PRIMARY KEY,            -- Asia/Shanghai 日
    alerts_n          INT NOT NULL DEFAULT 0,      -- 当日告警数（成熟样本口径）

    win_1h  NUMERIC(6,4),  win_4h  NUMERIC(6,4),  win_12h NUMERIC(6,4),  win_24h NUMERIC(6,4),
    odds_1h NUMERIC(8,4),  odds_4h NUMERIC(8,4),  odds_12h NUMERIC(8,4), odds_24h NUMERIC(8,4),
    be_1h   NUMERIC(6,4),  be_4h   NUMERIC(6,4),  be_12h   NUMERIC(6,4),  be_24h   NUMERIC(6,4),
    pf_1h   NUMERIC(8,4),  pf_4h   NUMERIC(8,4),  pf_12h   NUMERIC(8,4),  pf_24h   NUMERIC(8,4),
    avg_1h  NUMERIC(10,4), avg_4h  NUMERIC(10,4), avg_12h  NUMERIC(10,4), avg_24h  NUMERIC(10,4),

    btc_win_24h    NUMERIC(6,4),                   -- BTC 同方向 T+24h 胜率（beta 基准）
    btc_avg_24h    NUMERIC(10,4),
    excess_avg_24h NUMERIC(10,4),                  -- 平均超额（alpha）

    -- 行情环境
    btc_close         NUMERIC(20,8),
    btc_chg_pct       NUMERIC(8,4),                -- 当日涨跌（%）
    btc_amp_pct       NUMERIC(8,4),                -- 当日振幅 (high-low)/low（%）
    btc_1h_gt05_ratio NUMERIC(6,4),                -- 小时 |涨跌|>0.5% 的占比
    fgi               INT,
    cap_trend_pct     NUMERIC(8,4),
    regime_label      TEXT,                        -- trend / range / mixed

    -- 近 3 日滚动（失配判定用，抗单日小样本噪声）
    roll3_alerts_avg NUMERIC(8,2),
    roll3_win_1h     NUMERIC(6,4),
    roll3_be_1h      NUMERIC(6,4),
    roll3_pf_1h      NUMERIC(8,4),

    -- 分桶诊断摘要
    n_buckets        INT DEFAULT 0,
    edge_buckets     JSONB,                        -- [{dim,bucket,n,win,be,share}]
    top_share_bucket JSONB,

    -- 样本成熟度
    matured_n     INT DEFAULT 0,
    pending_n     INT DEFAULT 0,
    sample_ready  BOOLEAN NOT NULL DEFAULT FALSE,  -- matured_n >= 10

    -- 失配判定
    mismatch_flag  BOOLEAN NOT NULL DEFAULT FALSE,
    mismatch_rules TEXT[],                         -- A/B/C/D/E
    severity       TEXT NOT NULL DEFAULT 'ok',     -- ok / watch / high
    conclusion     TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.scan_edge_daily IS
    '告警胜率赔率日报聚合：多窗口胜率/赔率/PF/盈亏平衡线 + 行情环境 + 阈值-行情失配判定';

-- 3. 分桶诊断
CREATE TABLE IF NOT EXISTS biz.scan_edge_bucket (
    report_date DATE NOT NULL,
    dim         TEXT NOT NULL,   -- vol_ratio | price_chg | oi_chg | timeframe | scenario | regime | confidence | pool
    bucket      TEXT NOT NULL,
    n           INT NOT NULL DEFAULT 0,
    win_1h      NUMERIC(6,4),
    win_4h      NUMERIC(6,4),
    odds_1h     NUMERIC(8,4),
    be_1h       NUMERIC(6,4),
    pf_1h       NUMERIC(8,4),
    avg_1h      NUMERIC(10,4),
    avg_24h     NUMERIC(10,4),
    share       NUMERIC(6,4),    -- 占当日告警数比例
    edge        BOOLEAN NOT NULL DEFAULT FALSE,  -- n>=5 且 win_1h<be_1h 且 share>=0.2
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (report_date, dim, bucket)
);

CREATE INDEX IF NOT EXISTS idx_scan_edge_bucket_edge
    ON biz.scan_edge_bucket (edge, report_date DESC);

COMMENT ON TABLE biz.scan_edge_bucket IS
    '告警分桶诊断（当日 × 维度 × 桶），用于定位「该收紧哪个阈值」的具体坐标';
