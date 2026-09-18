-- =====================================================================
-- 催化剂后验验证体系（P0）：结局追踪 + 权重校准 + 变更审计
-- 参考《催化剂实测评分与反馈校准方案_2026-09-17.md》v1.1
-- 功能：
--   1. catalyst_outcome    催化剂价格结局追踪（多窗口超额收益 + 命中判定）
--   2. catalyst_calibration 各维度实测权重（hit_rate/avg_excess → calibrated_score）
--   3. calibration_log      权重变更审计
-- 编号：fix_047
-- 日期：2026-09-17
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. 催化剂价格结局追踪表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.catalyst_outcome (
    outcome_id      BIGSERIAL PRIMARY KEY,
    catalyst_id     BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,

    -- 基线时点（信号生成时刻 = 交易决策起点）
    signal_id       BIGINT REFERENCES biz.catalyst_signal(signal_id) ON DELETE SET NULL,
    base_time       TIMESTAMPTZ NOT NULL,     -- = catalyst_signal.created_at
    base_price_usd  NUMERIC(20,10),           -- 基线价（发布后第一个可得价）

    -- 各窗口绝对收益（%）：L1 用 K 线精确，L2 无小时级数据记 NULL
    ret_4h          NUMERIC(10,4),            -- L1: 4h K线 / L2: NULL
    ret_24h         NUMERIC(10,4),            -- L1: 24h K线 / L2: CMC近似
    ret_72h         NUMERIC(10,4),            -- L1: 72h K线 / L2: NULL
    ret_7d          NUMERIC(10,4),            -- market_daily
    ret_14d         NUMERIC(10,4),            -- market_daily

    -- 各窗口超额收益（扣 BTC beta，%）：同源计算
    excess_4h       NUMERIC(10,4),
    excess_24h      NUMERIC(10,4),
    excess_72h      NUMERIC(10,4),
    excess_7d       NUMERIC(10,4),
    excess_14d      NUMERIC(10,4),

    -- 命中判定（相对 impact_direction，缺失时用 event_type 默认方向）
    hit_4h          BOOLEAN,
    hit_24h         BOOLEAN,
    hit_72h         BOOLEAN,
    hit_7d          BOOLEAN,

    -- 量能验证
    vol_ratio_24h   NUMERIC(10,4),            -- 24h量 / 7日均量
    vol_ratio_72h   NUMERIC(10,4),

    -- 极端风险
    max_drawdown_24h NUMERIC(10,4),           -- 24h内最大回撤（%）
    max_gain_24h    NUMERIC(10,4),            -- 24h内最大涨幅（%）

    -- 元信息
    impact_direction TEXT,                    -- 冗余快照：bullish/bearish/neutral（可空）
    direction_src   TEXT,                     -- impact 表 | event_type默认 | null
    data_tier       TEXT NOT NULL DEFAULT 'L2',  -- L1(有K线) | L2(无K线)
    outcome_state   TEXT NOT NULL DEFAULT 'pending',
                    -- pending(未到最终窗口) / resolved(已结算) / no_data(无行情)
    last_window     SMALLINT DEFAULT 4,       -- 已结算的最大窗口小时数（4/24/72/168/336）
    ret_source      TEXT DEFAULT 'klines',    -- klines | market_daily | cmc_snapshot
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (catalyst_id, asset_id)
);

COMMENT ON TABLE biz.catalyst_outcome
    IS '催化剂价格结局追踪（后验验证）：多窗口绝对/超额收益 + 方向命中判定';
COMMENT ON COLUMN biz.catalyst_outcome.data_tier
    IS '数据分层：L1(有币安K线,小时级精确) / L2(无K线,仅24h近似+日频)';
COMMENT ON COLUMN biz.catalyst_outcome.outcome_state
    IS '结算状态：pending(未到最终窗口) / resolved(已结算) / no_data(无行情)';
COMMENT ON COLUMN biz.catalyst_outcome.last_window
    IS '已结算的最大窗口小时数（4/24/72/168/336），336h(14d)后置 resolved';

CREATE INDEX IF NOT EXISTS idx_catalyst_outcome_cat
    ON biz.catalyst_outcome (catalyst_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_outcome_asset
    ON biz.catalyst_outcome (asset_id, outcome_state);
CREATE INDEX IF NOT EXISTS idx_catalyst_outcome_state
    ON biz.catalyst_outcome (outcome_state, last_window);

-- ---------------------------------------------------------------------
-- 2. 校准权重表（各维度实测权重）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.catalyst_calibration (
    calib_id        BIGSERIAL PRIMARY KEY,
    dim             TEXT NOT NULL,   -- event_type | source | scope | resonance_band
    dim_value       TEXT NOT NULL,   -- listing / binance_listing / single_pair / 80-100

    sample_count    INT NOT NULL DEFAULT 0,
    hit_rate        NUMERIC(6,4),    -- 方向命中率（bullish→涨，bearish→跌）
    avg_excess_24h  NUMERIC(10,4),   -- 24h平均超额收益
    avg_excess_72h  NUMERIC(10,4),
    median_excess_72h NUMERIC(10,4),
    win_rate_72h    NUMERIC(6,4),    -- 72h 方向正确率
    ic_24h          NUMERIC(8,4),    -- 信息系数：评分与24h超额的相关
    calibrated_score SMALLINT,       -- 校准后权重 0-100
    prior_score     SMALLINT,        -- 原先验权重（对比用）
    weight_mode     TEXT NOT NULL DEFAULT 'prior',  -- prior(样本<30) / calibrated
    window_start    DATE NOT NULL,   -- 校准窗口起点
    window_end      DATE NOT NULL,   -- 校准窗口终点
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (dim, dim_value, window_end)
);

COMMENT ON TABLE biz.catalyst_calibration
    IS '催化剂评分校准权重（实测命中率/超额收益 → calibrated_score）';
COMMENT ON COLUMN biz.catalyst_calibration.weight_mode
    IS '权重模式：prior(样本<30用先验) / calibrated(样本≥30用实测)';

CREATE INDEX IF NOT EXISTS idx_catalyst_calib_dim
    ON biz.catalyst_calibration (dim, dim_value, window_end DESC);

-- ---------------------------------------------------------------------
-- 3. 权重变更审计表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.calibration_log (
    log_id      BIGSERIAL PRIMARY KEY,
    calib_id    BIGINT REFERENCES biz.catalyst_calibration(calib_id),
    dim         TEXT NOT NULL,
    dim_value   TEXT NOT NULL,
    prior_score SMALLINT,
    new_score   SMALLINT,
    changed_by  TEXT NOT NULL DEFAULT 'auto',  -- auto | manual
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.calibration_log
    IS '校准权重变更审计（谁改的、从几分到几分、为什么）';

CREATE INDEX IF NOT EXISTS idx_calibration_log_dim
    ON biz.calibration_log (dim, dim_value, created_at DESC);
