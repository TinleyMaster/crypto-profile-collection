-- fix_070_signal_type_calibration.sql
-- 刀2（2026-09-26 审计·P0-B）：打通「回测 → 权重」反馈闭环。
--   · 回测（backtest_opportunities.py）按 signal_type 产出命中率 / alpha，落本表；
--   · macro_market.py 启动时读本表最新窗口，对「低命中率」或「样本不足」的类型
--     做 conviction 衰减（分数 ×weight_factor）并禁止其进入 HIGH 候选；
--   · NOT_CALIBRABLE / NOT_BACKTESTABLE（聚合类、非可交易 target）标记为豁免，
--     不降权、保留 HIGH——无样本 ≠ 表现差，不应惩罚。
-- 幂等：CREATE TABLE IF NOT EXISTS，可重复执行。

CREATE TABLE IF NOT EXISTS biz.signal_type_calibration (
    calib_id        BIGSERIAL PRIMARY KEY,
    signal_type     TEXT NOT NULL,
    horizon_days    INT  NOT NULL,                    -- 主指标持有期（天，取 SIGNAL_HORIZONS 最短档）
    sample_count    INT  NOT NULL DEFAULT 0,          -- 有效取价样本数（方向已计入 pnl）
    wins            INT  NOT NULL DEFAULT 0,
    losses          INT  NOT NULL DEFAULT 0,
    hit_rate        NUMERIC(6,4),                     -- wins / sample_count
    avg_pnl_pct     NUMERIC(10,4),                    -- 平均方向调整后收益（%）
    avg_alpha       NUMERIC(10,4),                    -- 平均相对 BTC 超额（%）
    -- D1/D2 缺口留痕：回测丢弃样本必须可追溯，禁止静默丢
    skipped_dup              INT NOT NULL DEFAULT 0,  -- 同快照同 target 去重丢弃
    skipped_no_price         INT NOT NULL DEFAULT 0,  -- 日价缺口（D1）
    skipped_not_backtestable INT NOT NULL DEFAULT 0,  -- 不可回测类型（D2）
    weight_factor   NUMERIC(4,2) NOT NULL DEFAULT 1.00,  -- 评分衰减系数（0.60 / 1.00）
    gate            TEXT NOT NULL,                    -- calibrated_ok | calibrated_low | preliminary
                                                      -- | exempt_not_calibrable | exempt_not_backtestable
    no_high         BOOLEAN NOT NULL DEFAULT FALSE,   -- TRUE → 不进 HIGH 候选（仅降档，不删卡）
    window_start    DATE NOT NULL,
    window_end      DATE NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (signal_type, horizon_days, window_end)
);

COMMENT ON TABLE biz.signal_type_calibration
    IS '高亮信号 signal_type 级回测校准（命中率/alpha → 权重衰减系数），由 backtest_opportunities.py 写入、macro_market.py 消费';
COMMENT ON COLUMN biz.signal_type_calibration.weight_factor
    IS '评分衰减系数：命中率<0.5 或样本<MIN_SAMPLES(30) → 0.60，其余 1.00（豁免类型恒 1.00）';
COMMENT ON COLUMN biz.signal_type_calibration.gate
    IS '门控状态：calibrated_ok/calibrated_low/preliminary/exempt_not_calibrable/exempt_not_backtestable';
COMMENT ON COLUMN biz.signal_type_calibration.no_high
    IS 'TRUE = 该类型不得进入 HIGH 候选（分数衰减后档位封顶 MED，卡片保留不删）';

CREATE INDEX IF NOT EXISTS idx_signal_type_calibration_latest
    ON biz.signal_type_calibration (signal_type, window_end DESC);