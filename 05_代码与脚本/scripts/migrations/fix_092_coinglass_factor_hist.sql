-- ============================================================
-- fix_092_coinglass_factor_hist.sql
-- Coinglass 因子历史扩展表（OI 全年 / 基差 / 全市场聚合爆仓）
--   biz.cg_oi_hist        OI 聚合历史（OHLC，粒度 ≥4h）
--   biz.cg_basis_hist     期货基差历史（永续-现货价差，粒度 ≥4h）
--   biz.cg_liq_agg_hist   多所聚合爆仓历史（long/short 分列）
-- 背景：现有 oi_cvd_snapshot 仅 5m 自采 41 天；本表用 Coinglass Hobbyist
--       回填 @4h=180 天 / @12h=360 天，支撑全年因子回测。
--       独立表（不并入现役 5m 表），符合「只新增不改既有」红线。
-- 注意：symbol 一律存**合约码**（BTCUSDT），API 层负责基码转换。
-- ============================================================

-- 1. OI 聚合历史（open-interest/aggregated-history）
CREATE TABLE IF NOT EXISTS biz.cg_oi_hist (
    symbol    TEXT        NOT NULL,          -- 合约符号（BTCUSDT）
    interval  TEXT        NOT NULL,          -- 4h / 6h / 8h / 12h / 1d
    ts        TIMESTAMPTZ NOT NULL,          -- 桶起始时间
    oi_open   NUMERIC(30,2),                 -- OI 名义价值（USD）
    oi_high   NUMERIC(30,2),
    oi_low    NUMERIC(30,2),
    oi_close  NUMERIC(30,2),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_cg_oi_hist_sym_iv_ts
    ON biz.cg_oi_hist (symbol, interval, ts DESC);

COMMENT ON TABLE biz.cg_oi_hist IS
    'Coinglass OI 聚合历史（粒度≥4h）；oi_close 环比=OI 变化率因子';

-- 2. 基差历史（basis/history）
CREATE TABLE IF NOT EXISTS biz.cg_basis_hist (
    symbol      TEXT        NOT NULL,        -- 合约符号（BTCUSDT）
    interval    TEXT        NOT NULL,        -- 4h / 6h / 8h / 12h / 1d
    ts          TIMESTAMPTZ NOT NULL,        -- 桶起始时间
    open_basis  NUMERIC(12,4),               -- 开桶基差（%）
    close_basis NUMERIC(12,4),               -- 闭桶基差（%）
    open_change NUMERIC(12,2),               -- 开桶基差变化（bp）
    close_change NUMERIC(12,2),              -- 闭桶基差变化（bp）
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_cg_basis_hist_sym_iv_ts
    ON biz.cg_basis_hist (symbol, interval, ts DESC);

COMMENT ON TABLE biz.cg_basis_hist IS
    'Coinglass 期货基差历史（永续-现货价差，粒度≥4h）；close_basis=多头溢价情绪因子';

-- 3. 多所聚合爆仓历史（liquidation/aggregated-history）
CREATE TABLE IF NOT EXISTS biz.cg_liq_agg_hist (
    symbol    TEXT        NOT NULL,          -- 合约符号（BTCUSDT）
    interval  TEXT        NOT NULL,          -- 4h / 6h / 8h / 12h / 1d
    ts        TIMESTAMPTZ NOT NULL,          -- 桶起始时间
    long_liq_usd  NUMERIC(24,2),             -- 多头爆仓额（USD，价格下跌被强平）
    short_liq_usd NUMERIC(24,2),             -- 空头爆仓额（USD，价格上涨被强平）
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_cg_liq_agg_sym_iv_ts
    ON biz.cg_liq_agg_hist (symbol, interval, ts DESC);

COMMENT ON TABLE biz.cg_liq_agg_hist IS
    'Coinglass 多所聚合爆仓历史（粒度≥4h）；long/short 分列，可算爆仓不对称因子';
