-- ============================================================
-- fix_091_coinglass_hist.sql
-- Coinglass Hobbyist 历史回填独立表（CVD 净主动 + 多空比）
--   biz.cg_taker_volume_hist  主动买卖量历史（CVD 源，粒度 ≥4h）
--   biz.cg_ls_ratio_hist      多空比历史（top_position / top_account / global_account）
-- 背景：现有 5m 自采（oi_cvd_snapshot 2026-09-22 起 / long_short_ratio 按需采集）
--       历史太短；Coinglass Hobbyist 可回填 @4h=180 天、@12h=360 天。
--       独立表（不并入现役 5m 表），符合「只新增不改既有」红线。
-- ============================================================

-- 1. 主动买卖量历史（CVD 源）
CREATE TABLE IF NOT EXISTS biz.cg_taker_volume_hist (
    symbol        TEXT        NOT NULL,          -- 合约符号（BTCUSDT）
    interval      TEXT        NOT NULL,          -- 4h / 6h / 8h / 12h / 1d
    ts            TIMESTAMPTZ NOT NULL,          -- 桶起始时间
    taker_buy_usd NUMERIC(30,4),                 -- 主动买量（单位疑似百万 USD，仅用比值）
    taker_sell_usd NUMERIC(30,4),                -- 主动卖量
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_cg_taker_hist_sym_iv_ts
    ON biz.cg_taker_volume_hist (symbol, interval, ts DESC);

COMMENT ON TABLE biz.cg_taker_volume_hist IS
    'Coinglass 主动买卖量历史（CVD 源，粒度≥4h）；taker_net_ratio=(buy-sell)/(buy+sell) 跨币可比';

-- 2. 多空比历史（top_position / top_account / global_account）
CREATE TABLE IF NOT EXISTS biz.cg_ls_ratio_hist (
    symbol      TEXT        NOT NULL,            -- 合约符号（BTCUSDT）
    interval    TEXT        NOT NULL,            -- 4h / 6h / 8h / 12h / 1d
    ratio_type  TEXT        NOT NULL,            -- top_position / top_account / global_account
    ts          TIMESTAMPTZ NOT NULL,            -- 桶起始时间
    long_percent  NUMERIC(10,4),                 -- 多头占比 %
    short_percent NUMERIC(10,4),                 -- 空头占比 %
    ls_ratio      NUMERIC(12,6),                 -- 多空比（long/short）
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, ratio_type, ts)
);
CREATE INDEX IF NOT EXISTS idx_cg_ls_hist_sym_iv_rt_ts
    ON biz.cg_ls_ratio_hist (symbol, interval, ratio_type, ts DESC);

COMMENT ON TABLE biz.cg_ls_ratio_hist IS
    'Coinglass 多空比历史（top_position=顶级交易员持仓量/top_account=账户数/global_account=全体账户），粒度≥4h';
