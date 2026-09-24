-- fix_069: CoinGlass 4h+ 爆仓历史（P1）—— 分段增量口径，仅供标定/回测消费
--
-- ⚠️ 首行用途限制：本表仅供 workbench/calib_squeeze_liq_thr.py、backtest_* 一类
--    标定/回测脚本消费；**禁止接入 scan_squeeze / squeeze_fuel 的任何实时判定分支**
--    （粒度不匹配：本表最小 4h，实时判定窗口为 5m）。
-- ⚠️ 口径：本表是**分段增量**（每个区间内新增的爆仓额），与 biz.liquidation_snapshot
--    的**滚动窗口绝对值**口径不可换算、不可相加、严禁跨桶差分。
--    两套口径被混用即重演 2026-09-21 审计 P1-1 的「假 0」缺陷。
--
-- 关联方案：04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md §5.2 / §4.4
-- 幂等：CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS，可重复执行。
--
-- 应用方式（幂等，可重复跑）：
--   python 05_代码与脚本/scripts/apply_migration.py fix_069_liquidation_history.sql

-- 1. 4h+ 爆仓历史（分段增量口径，与 liquidation_snapshot 的滚动窗口严格区分）
CREATE TABLE IF NOT EXISTS biz.liquidation_history (
    symbol          TEXT        NOT NULL,        -- 本库合约码，如 BTCUSDT（与 biz.liquidation_snapshot.symbol 同口径；≠ coin-list 返回的基码 BTC）
    interval        TEXT        NOT NULL,        -- 4h/6h/8h/12h/1d，显式存，禁混桶
    exchange_scope  TEXT        NOT NULL,        -- 'binance' / 'all'，口径列，禁混算
    ts              TIMESTAMPTZ NOT NULL,        -- 区间起点（UTC，interval 对齐）
    long_liq_usd    NUMERIC(24,2),               -- 该区间内多单爆仓额（分段增量）
    short_liq_usd   NUMERIC(24,2),
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, exchange_scope, ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_history_sym_iv_scope_ts
    ON biz.liquidation_history (symbol, interval, exchange_scope, ts DESC);

COMMENT ON TABLE biz.liquidation_history IS
  'CoinGlass 4h+ 爆仓历史（分段增量，非滚动窗口）。'
  '⚠️ 仅供标定/回测消费，禁止接入 scan_squeeze / squeeze_fuel 实时判定；'
  '⚠️ 与 biz.liquidation_snapshot 的滚动窗口口径不可换算、不可相加。';

COMMENT ON COLUMN biz.liquidation_history.symbol IS
  '本库合约码（BTCUSDT / 1000PEPEUSDT），与 biz.liquidation_snapshot.symbol 同口径。'
  '写入侧由请求参数原样落库：scope=binance 走 liquidation/history（交易对级，直传合约码）；'
  'scope=all 走 liquidation/aggregated-history（币种级）⇒ 先按「去 USDT 后缀」映射为币种基码'
  '（BTC / 1000PEPE）再请求，落库仍写回合约码。';
COMMENT ON COLUMN biz.liquidation_history.interval IS
  '粒度（Hobbyist 套餐下限 4h）。进 PK ⇒ 4h 与 1d 结构上不可能混桶。';
COMMENT ON COLUMN biz.liquidation_history.exchange_scope IS
  'binance = 单所（liquidation/history, exchange=Binance）；'
  'all = 多所聚合（liquidation/aggregated-history, exchange_list=supported-exchanges 全量）。'
  '进 PK ⇒ 两口径结构上不可能混算（§4.3 口径 A/B 分离）。';
COMMENT ON COLUMN biz.liquidation_history.ts IS
  '区间**起点**（UTC，与 interval 对齐，如 4h 桶的 00:00/04:00/…）。'
  '⚠️ 最新一个桶是**正在累积**的未完成区间，重跑即覆盖（PK + ON CONFLICT DO UPDATE）。';
COMMENT ON COLUMN biz.liquidation_history.long_liq_usd IS
  '该区间内**多单**爆仓额（分段增量）。NULL = 该区间未取到数据，**不得补 0**。'
  '与 biz.liquidation_snapshot.long_liq_usd_1h（滚动 1h 窗口绝对值）不可换算。';
COMMENT ON COLUMN biz.liquidation_history.short_liq_usd IS
  '该区间内**空单**爆仓额（分段增量）。NULL = 该区间未取到数据，**不得补 0**。'
  '与 biz.liquidation_snapshot.short_liq_usd_1h（滚动 1h 窗口绝对值）不可换算。';

-- 保留策略（§5.2）：本表**不纳入 prune_scan_data 清理**（体量可控且为回测资产）；
-- 若日后体量增长，再按 interval 分档处理（同 prune_scan_data 的既有分档风格）。

-- 2. 回填游标（容器重启后 --resume 用）
CREATE TABLE IF NOT EXISTS biz.liquidation_backfill_cursor (
    symbol          TEXT        NOT NULL,
    interval        TEXT        NOT NULL,
    exchange_scope  TEXT        NOT NULL,
    done_through    TIMESTAMPTZ NOT NULL,        -- 已成功覆盖到的区间起点（含）
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, exchange_scope)
);

COMMENT ON TABLE biz.liquidation_backfill_cursor IS
  '4h 爆仓历史回填游标（P1）。回填作业必然跨容器执行窗口（88min ≫ 实例寿命 ≈74.6min），'
  '--resume 依据本表的 done_through 跳过已覆盖区间。'
  '⚠️ 仅在**整币全区间成功**后推进（失败/截断不留游标，避免把半截数据当成已完成）。';
COMMENT ON COLUMN biz.liquidation_backfill_cursor.done_through IS
  '已成功覆盖到的**区间起点（含）**，取值 = 本次实际落库的最早 ts（不是窗口起点 NOW()-days，'
  '因为接口 @4h 只回 180 天，请求窗口更大时实际最早点会更晚）。';