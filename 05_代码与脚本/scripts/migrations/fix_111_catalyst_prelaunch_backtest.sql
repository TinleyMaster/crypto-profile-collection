-- =====================================================================
-- 催化剂埋伏回测体系：事前窗口 vs 事后窗口收益对比
-- 目的：验证「埋伏催化剂」（事件前买、事件时卖）vs「追涨」（事件时买、事后卖）的收益差异
-- 编号：fix_111
-- 日期：2026-10-19
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.catalyst_prelaunch_backtest (
    pre_id          BIGSERIAL PRIMARY KEY,
    catalyst_id     BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id        BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,

    -- 基线时点（= asset_catalyst.published_at，事件发布时刻 = t=0）
    base_time       TIMESTAMPTZ NOT NULL,
    base_price_usd  NUMERIC(20,10),           -- t=0 的 K 线收盘价

    -- ── 事前窗口（埋伏：t=-N 买入 → t=0 卖出，收益= price(t=0)/price(t=-N) - 1）──
    pre_ret_1d      NUMERIC(10,4),            -- 事件前 1d 埋伏绝对收益 (%)
    pre_ret_3d      NUMERIC(10,4),            -- 事件前 3d 埋伏绝对收益 (%)
    pre_ret_7d      NUMERIC(10,4),            -- 事件前 7d 埋伏绝对收益 (%)

    pre_excess_1d   NUMERIC(10,4),            -- 事前 1d 扣 BTC 超额 (%)
    pre_excess_3d   NUMERIC(10,4),            -- 事前 3d 扣 BTC 超额 (%)
    pre_excess_7d   NUMERIC(10,4),            -- 事前 7d 扣 BTC 超额 (%)

    -- ── 事后窗口（追涨：t=0 买入 → t=+N 卖出，同 catalyst_outcome 口径，方便直接对比）──
    post_ret_1d     NUMERIC(10,4),            -- 事件后 1d 绝对收益 (%)
    post_ret_3d     NUMERIC(10,4),            -- 事件后 3d 绝对收益 (%)
    post_ret_7d     NUMERIC(10,4),            -- 事件后 7d 绝对收益 (%)

    post_excess_1d  NUMERIC(10,4),            -- 事后 1d 扣 BTC 超额 (%)
    post_excess_3d  NUMERIC(10,4),            -- 事后 3d 扣 BTC 超额 (%)
    post_excess_7d  NUMERIC(10,4),            -- 事后 7d 扣 BTC 超额 (%)

    -- ── 事件元信息（冗余快照，方便分组分析）──
    event_type      TEXT,                     -- listing / partnership / security ...
    impact_direction TEXT,                    -- bullish / bearish / neutral (可空)
    impact_src      TEXT,                     -- impact 表 | event_type 默认 | null
    event_month     CHAR(7),                  -- YYYY-MM，分组用

    -- ── 数据质量 ──
    data_tier       TEXT NOT NULL,            -- L1(有K线) | L2(仅日频)
    has_pre_7d      BOOLEAN DEFAULT FALSE,    -- 事件前 7d K 线是否完整
    has_post_7d     BOOLEAN DEFAULT FALSE,    -- 事件后 7d K 线是否完整

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (catalyst_id, asset_id)
);

COMMENT ON TABLE biz.catalyst_prelaunch_backtest
    IS '催化剂埋伏回测：事前窗口收益 vs 事后窗口收益对比（t=0=事件发布）';
COMMENT ON COLUMN biz.catalyst_prelaunch_backtest.pre_ret_7d
    IS '埋伏策略：事件前 7d 买入、事件时卖出的绝对收益 (%)';
COMMENT ON COLUMN biz.catalyst_prelaunch_backtest.post_ret_7d
    IS '追涨策略：事件时买入、事件后 7d 卖出的绝对收益 (%)';

CREATE INDEX IF NOT EXISTS idx_cat_prelaunch_event_month
    ON biz.catalyst_prelaunch_backtest (event_month, event_type);
CREATE INDEX IF NOT EXISTS idx_cat_prelaunch_type
    ON biz.catalyst_prelaunch_backtest (event_type);
CREATE INDEX IF NOT EXISTS idx_cat_prelaunch_asset
    ON biz.catalyst_prelaunch_backtest (asset_id);
