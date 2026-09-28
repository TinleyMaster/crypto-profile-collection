-- fix_077: CoinGlass V4 跨所衍生品快照（CGV4-003）—— OI（聚合/分所/币本位）+ 资金费率 + 多空比
--
-- 用途：用 CoinGlass V4 **跨所聚合**衍生品数据，绕开 Binance fapi 单所免费源在当前 IDC
--   出口被 418 封禁的结构性问题（工单 CGV4-003）。本表为**并行源**，不改动既有
--   binance fapi / biz.asset_derivatives 链路（工单红线⑤：仅新增）。
--
-- ⚠️ 口径：
--   - `symbol` 为**币种基码**（BTC / 1000PEPE），与 CoinGlass 请求口径一致；
--     ≠ biz.liquidation_history.symbol（合约码 BTCUSDT）。
--   - `exchange = 'All'` 是接口返回的**跨所聚合行**，非本项目自算；与各所分列行共存，
--     消费时**按需择一**，严禁把 'All' 行与分所行相加（重复计数）。
--   - `oi_*` 为**当期快照**（非分段增量、非滚动窗口），时间序列由多次采集累积；
--     严谨的「OI 变化」请用 `open_interest_change_percent_*` 字段或跨 ts 自查，勿跨表换算。
--
-- 关联：04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md；工单 CGV4-003。
-- 幂等：CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS，可重复执行。
--
-- 应用方式（幂等，可重复跑）：
--   python 05_代码与脚本/scripts/apply_migration.py fix_077_coinglass_derivatives_snapshot.sql

CREATE TABLE IF NOT EXISTS biz.coinglass_derivatives_snapshot (
    symbol                  TEXT        NOT NULL,   -- 币种基码（BTC / 1000PEPE）
    exchange                TEXT        NOT NULL,   -- 交易所名；'All' = 接口给的跨所聚合行
    ts                      TIMESTAMPTZ NOT NULL,   -- 快照采集时刻（分钟对齐）
    oi_usd                  NUMERIC(28,4),          -- 未平仓合约名义价值（USD）
    oi_quantity             NUMERIC(28,4),          -- 未平仓合约数量（币）
    oi_coin_margin_usd      NUMERIC(28,4),          -- 币本位 OI（USD）
    oi_stablecoin_margin_usd NUMERIC(28,4),         -- 稳定币本位 OI（USD）
    oi_change_24h_pct       NUMERIC(12,4),          -- 24h OI 变化率（%）
    funding_rate            NUMERIC(20,10),         -- 当期资金费率（小数，如 0.000129）
    funding_rate_interval_h NUMERIC(8,2),           -- 结算间隔（小时）
    next_funding_time       TIMESTAMPTZ,            -- 下次结算时刻
    ls_global_long_pct      NUMERIC(8,4),           -- 全站账户多单占比（%）
    ls_global_short_pct     NUMERIC(8,4),
    ls_global_ratio         NUMERIC(12,6),
    ls_top_account_long_pct NUMERIC(8,4),           -- 顶级交易员账户多单占比（%）
    ls_top_account_short_pct NUMERIC(8,4),
    ls_top_account_ratio    NUMERIC(12,6),
    ls_top_position_long_pct NUMERIC(8,4),          -- 顶级交易员持仓多单占比（%）
    ls_top_position_short_pct NUMERIC(8,4),
    ls_top_position_ratio   NUMERIC(12,6),
    source                  TEXT        NOT NULL DEFAULT 'coinglass_v4',
    fetched_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, exchange, ts)
);
CREATE INDEX IF NOT EXISTS idx_cg_deriv_snap_sym_ts
    ON biz.coinglass_derivatives_snapshot (symbol, ts DESC);

COMMENT ON TABLE biz.coinglass_derivatives_snapshot IS
  'CoinGlass V4 跨所衍生品快照（OI 聚合/分所/币本位 + 资金费率 + 多空比）。'
  '⚠️ 并行源，不改既有 binance fapi / biz.asset_derivatives 链路；'
  '⚠️ exchange=''All'' 为接口给的跨所聚合行，与分所行严禁相加。';
COMMENT ON COLUMN biz.coinglass_derivatives_snapshot.symbol IS
  '币种基码（BTC / 1000PEPE），与 CoinGlass OI/funding 请求口径一致；'
  '≠ biz.liquidation_history.symbol（合约码 BTCUSDT）。';
COMMENT ON COLUMN biz.coinglass_derivatives_snapshot.exchange IS
  '交易所名；"All" = 接口返回的跨所聚合行（非本项目自算）。消费时按需择一，严禁与分所行相加。';
COMMENT ON COLUMN biz.coinglass_derivatives_snapshot.ts IS
  '快照采集时刻（分钟对齐）。oI 为当期快照、非分段增量/滚动窗口，时间序列靠多次采集累积。';
COMMENT ON COLUMN biz.coinglass_derivatives_snapshot.funding_rate IS
  '当期资金费率（小数，如 0.000129 = 0.0129%），取自 funding-rate/exchange-list（无参一次拉全）。';