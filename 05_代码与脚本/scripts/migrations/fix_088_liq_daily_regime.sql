-- fix_088: 爆仓极值日窗口覆盖层（biz.liq_daily_regime）
--
-- 背景（2026-10-03 投研结论落地）：BTC/ETH 日频回测结论——
--   空爆主导日（爆仓/成交额近 90 日 p90+ 且空单占比 ≥70%）后 7-14 日显著上涨（胜率 61%）；
--   多空双爆日偏涨（恐慌顶点）；多爆主导日无方向（不产生信号）。
-- 本表把「极值日」与「可行动窗口」落成**日频环境覆盖层**，供早报/告警邮件只读展示。
--
-- ⚠️ 用途限制（铁律）：
--   - **仅供标定/回测/展示消费**（早报一行、扫描告警环境标注），**禁止接入 scan_daemon
--     的实时判定/置信度计算**（日频粒度不匹配 5m 判定窗口；`_build_regime` 只读本表的
--     bucket/窗口标注追加 tags，不改 long_fav/short_fav）。
--   - 口径与 workbench/backtest_liq_cascade_btc_eth.py 逐行一致（相对分位 + 方向主导）。
--
-- 幂等：CREATE TABLE IF NOT EXISTS，可重复执行。
-- 应用：python 05_代码与脚本/scripts/apply_migration.py fix_088_liq_daily_regime.sql

CREATE TABLE IF NOT EXISTS biz.liq_daily_regime (
    symbol       text          NOT NULL,   -- BTCUSDT / ETHUSDT
    ts           date          NOT NULL,   -- UTC 日期（当日 0 点~24 点，闭市判定）
    liq_total    numeric(24,2),            -- 当日爆仓额（binance 单所口径）
    liq_ratio    numeric(16,8),            -- 爆仓 / 当日成交额
    pct          numeric(8,4),             -- 近 90 日相对分位（含当日）
    long_share   numeric(8,4),             -- 多单占爆仓比例
    bucket       text,                     -- EXT-LONG / EXT-SHORT / EXT-MIX / NULL(非极值)
    is_extreme   boolean       NOT NULL DEFAULT false,  -- pct >= 0.90
    long_window  boolean       NOT NULL DEFAULT false,  -- 位于空爆极值日后的 7 个交易日内（做多有利窗口）
    capitulation_window boolean NOT NULL DEFAULT false, -- 位于双爆极值日后的 7 个交易日内（恐慌顶点观察）
    fwd7         numeric(12,6),            -- 后续 7 日收益（收盘→收盘）
    fwd14        numeric(12,6),            -- 后续 14 日收益
    fetched_at   timestamptz   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_daily_regime_sym_ts
    ON biz.liq_daily_regime (symbol, ts DESC);

COMMENT ON TABLE biz.liq_daily_regime IS
  'BTC/ETH 爆仓极值日窗口覆盖层（日频）。仅供标定/回测/展示消费；'
  '禁止接入实时判定（_build_regime 只读标注不改闸门）。口径与 backtest_liq_cascade 一致。';
COMMENT ON COLUMN biz.liq_daily_regime.bucket IS
  'EXT-SHORT=空爆主导（轧空→后续 7-14 日显著上涨）；EXT-LONG=多爆主导（无方向）；'
  'EXT-MIX=多空双爆（恐慌顶点→偏涨）。pct<0.90 时为 NULL。';
COMMENT ON COLUMN biz.liq_daily_regime.long_window IS
  '做多有利窗口：当日处于「空爆极值日」后 7 个交易日内（回测：H7 胜率 61%、均 +2.7%，H14 +6.25%）。';
COMMENT ON COLUMN biz.liq_daily_regime.capitulation_window IS
  '恐慌顶点观察窗口：当日处于「多空双爆日」后 7 个交易日内（回测：H7 +2.4%，偏涨）。';