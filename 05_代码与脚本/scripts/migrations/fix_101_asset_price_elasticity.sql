-- fix_101: 单币价格弹性快照表（投研 SOP 新增指标 P1.6）
--
-- 背景：有些币「好消息很多但价格不涨」（典型如 LINK）。本表把该现象量化。
-- 口径（method_version=v1）：
--   事件弹性 = 该币 L1 催化剂事件的 AR_72h 均值
--              AR = excess_72h − 当日全市场 L1 事件 excess_72h 中位
--              （excess_72h 已扣 BTC；再减当日中位剔除 alt 普涨 β）
--   独立事件 = (asset_id, base_time) 去重，同一时刻多条催化剂只算 1 个事件
--   弹性质量 = 24h 最大涨/最大跌的均值（上下行不对称）
--   基础相对强度 RS = 无事件窗口 7d/30d/90d 相对 BTC 超额收益
--   综合弹性分 0-100 + 评级（高/中高/低/负/样本不足/无数据）
--
-- 幂等：CREATE TABLE IF NOT EXISTS；唯一键 (asset_id, as_of)

CREATE TABLE IF NOT EXISTS biz.asset_price_elasticity (
    elasticity_id     bigserial   PRIMARY KEY,
    asset_id          bigint      NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    as_of             date        NOT NULL,
    method_version    text        NOT NULL DEFAULT 'v1',

    -- ── 事件弹性（主口径，源自 biz.catalyst_outcome L1） ──
    event_from        date,
    event_to          date,
    n_events          integer,          -- 独立事件数 ((asset_id, base_time) 去重)
    n_l1              integer,          -- L1（K线精确）事件数
    n_hit             integer,          -- 有命中判定的样本数
    ar_24h_mean       numeric(12,4),
    ar_72h_mean       numeric(12,4),    -- ★ 事件弹性主指标
    ar_72h_median     numeric(12,4),
    ar_7d_mean        numeric(12,4),
    hit_rate          numeric(6,4),
    hit_z             numeric(8,3),

    -- ── 弹性质量：上下行不对称 ──
    upside_mean_24h   numeric(12,4),    -- 平均 24h 最大涨(%)
    downside_mean_24h numeric(12,4),    -- 平均 24h 最大跌(%)，负值
    asym_ratio        numeric(8,3),     -- upside_mean / |downside_mean|

    -- ── 基础相对强度（无事件窗口，vs BTC） ──
    rs_7d             numeric(12,4),
    rs_30d            numeric(12,4),
    rs_90d            numeric(12,4),
    beta_btc          numeric(8,4),
    rs_source         text,             -- klines / market_daily / none

    -- ── 综合 ──
    elasticity_grade  text,             -- 高弹性/中高/低弹性/负弹性/样本不足/无数据
    elasticity_score  numeric(5,2),     -- 0-100
    by_event_type     jsonb,            -- 分事件类型 AR_72h 弹性

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_asset_price_elasticity UNIQUE (asset_id, as_of)
);

CREATE INDEX IF NOT EXISTS idx_asset_price_elasticity_asof
    ON biz.asset_price_elasticity (as_of DESC);

CREATE INDEX IF NOT EXISTS idx_asset_price_elasticity_grade
    ON biz.asset_price_elasticity (elasticity_grade);

COMMENT ON TABLE biz.asset_price_elasticity IS
  '单币价格弹性快照（投研 SOP P1.6）。事件弹性=该币催化剂事件 AR_72h 均值（AR=excess_72h−当日全市场中位）；RS=无事件窗口相对 BTC 超额。幂等键 asset_id+as_of。';
COMMENT ON COLUMN biz.asset_price_elasticity.ar_72h_mean IS '事件弹性主指标：独立事件 AR_72h 均值(%)';
COMMENT ON COLUMN biz.asset_price_elasticity.asym_ratio IS '上下行不对称：平均24h最大涨 / |平均24h最大跌|';
COMMENT ON COLUMN biz.asset_price_elasticity.elasticity_grade IS '弹性评级：高弹性/中高/低弹性/负弹性/样本不足/无数据';
COMMENT ON COLUMN biz.asset_price_elasticity.method_version IS '口径版本（v1=2026-10-16 首版）';
