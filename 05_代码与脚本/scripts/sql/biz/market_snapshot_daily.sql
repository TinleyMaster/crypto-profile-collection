-- 大盘每日快照（核心指标宽表）
-- 来源：macro_market.get_market_overview() 计算结果抽平
-- 用途：大盘趋势回溯、历史分位计算、周期分析、大盘热力图

CREATE TABLE IF NOT EXISTS biz.market_snapshot_daily (
    snapshot_date           DATE           NOT NULL PRIMARY KEY,

    -- 价格 / 市值
    btc_price               NUMERIC(12,2),
    btc_change_24h_pct      NUMERIC(10,4),
    eth_price               NUMERIC(12,2),
    eth_change_24h_pct      NUMERIC(10,4),
    total_market_cap_usd    NUMERIC(24,2),
    btc_dominance_pct       NUMERIC(6,2),
    total_volume_24h_usd    NUMERIC(24,2),

    -- 情绪
    fear_greed_value        INT,
    fear_greed_class        VARCHAR(20),
    altcoin_season_score    NUMERIC(6,2),
    cefi_index_value        NUMERIC(12,4),
    cefi_change_24h_pct     NUMERIC(10,4),

    -- 衍生品
    btc_open_interest_usd   NUMERIC(24,2),
    btc_funding_rate        NUMERIC(12,6),
    btc_oi_change_7d_pct    NUMERIC(10,4),

    -- 链上
    btc_mvrv_z_score        NUMERIC(8,4),
    btc_mvrv_pct_full       NUMERIC(6,4),
    btc_active_addresses    BIGINT,
    btc_realized_price_usd  NUMERIC(12,2),

    -- 稳定币
    stablecoin_total_supply_usd  NUMERIC(24,2),
    stablecoin_netflow_7d_usd    NUMERIC(24,2),
    stablecoin_flow_percentile   NUMERIC(6,4),

    -- ETF
    etf_total_flow_7d_usd        NUMERIC(24,2),
    btc_etf_flow_24h_usd         NUMERIC(24,2),

    -- 大盘综合评分
    overall_score               NUMERIC(6,2),
    emotion_subscore            NUMERIC(6,2),
    structure_subscore          NUMERIC(6,2),

    -- 周期
    btc_cycle_phase             VARCHAR(30),
    btc_cycle_label             VARCHAR(50),

    -- 大盘周期热力图（多维度整合）
    cycle_overall_heat          NUMERIC(6,2),       -- 综合周期热度 0-100
    cycle_phase                 VARCHAR(30),        -- 阶段 key（bear_floor / late_bear / early_bull / mid_bull / late_bull / bubble_top）
    cycle_phase_label           VARCHAR(60),        -- 阶段中文描述
    cycle_consistency_pct       NUMERIC(5,2),       -- 各维度阶段一致性 %

    -- 元数据
    raw_payload      JSONB,            -- 完整 overview JSON（可选，占用大但可回溯）
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_snapshot_date ON biz.market_snapshot_daily(snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_score ON biz.market_snapshot_daily(overall_score);
CREATE INDEX IF NOT EXISTS idx_market_snapshot_cycle ON biz.market_snapshot_daily(btc_cycle_phase);

COMMENT ON TABLE biz.market_snapshot_daily IS '大盘每日快照（核心指标宽表），供历史回溯、趋势分析、周期判断使用';
