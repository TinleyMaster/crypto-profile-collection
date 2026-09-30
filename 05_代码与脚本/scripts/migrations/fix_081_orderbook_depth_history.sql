-- fix_081: CoinGlass 单所（Binance）盘口深度历史 —— ±range 区间内累计挂单 USD
--
-- ⚠️ 首行用途限制：本表仅供 workbench/calib_*、backtest_* 一类**标定/回测**脚本消费；
--    **禁止接入 scan_daemon 的任何实时判定分支**（粒度错配：本表最小 4h，实时窗口为 5m/1h）。
-- ⚠️ 度量身份：本表是「±range_pct 价格区间内的**累计挂单 USD**」（静态盘口存量），
--    与 biz.asset_klines.quote_vol（成交额 = 流量）**不是同一量纲**，严禁换算/相加。
-- ⚠️ 本表**无 best bid/ask**（接口不返回）⇒ 价差（spread）维度不可由本表得出（§12.1-B8 只解「深度」半边）。
--
-- 关联工单：04_架构与代码方案/盘面扫描盘口深度接入工单_SCAN-LIQ-DEPTH-001_2026-09-30.md
-- 触发链：Coinglass套餐数据接入方案 §4.5 P2「orderbook/ask-bids-history」触发条件成立
--   （真正开始做设计文档 §7 B8「价差/深度过滤」）+ SCAN-LIQ-FILTER-001 §9.10 验证上限。
--
-- 幂等：CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS，可重复执行。
--
-- 应用方式（幂等，可重复跑）：
--   python 05_代码与脚本/scripts/apply_migration.py fix_081_orderbook_depth_history.sql

-- 1. 盘口深度历史（±range 累计挂单；静态存量口径，与成交额的流量口径严格区分）
CREATE TABLE IF NOT EXISTS biz.orderbook_depth_history (
    symbol          TEXT        NOT NULL,        -- 本库合约码，如 BTCUSDT（与 asset_klines / liquidation_history 同口径）
    interval        TEXT        NOT NULL,        -- 4h（HOBBYIST 粒度地板；本期只采 4h），显式存，禁混桶
    exchange_scope  TEXT        NOT NULL,        -- 'binance'（本期）/ 'all'（聚合端，未来扩展位）
    range_pct       NUMERIC(5,2) NOT NULL,       -- 深度档：1.00 = ±1%、0.25 = ±0.25% …；进 PK，多档共存禁混桶
    ts              TIMESTAMPTZ NOT NULL,        -- 区间起点（UTC，interval 对齐）
    bids_usd        NUMERIC(24,2),               -- ±range 内累计**买单**挂单 USD
    asks_usd        NUMERIC(24,2),               -- ±range 内累计**卖单**挂单 USD
    bids_quantity   NUMERIC(28,6),               -- ±range 内累计买单挂单数量（标的计）
    asks_quantity   NUMERIC(28,6),
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, exchange_scope, range_pct, ts)
);
CREATE INDEX IF NOT EXISTS idx_ob_depth_sym_iv_scope_rng_ts
    ON biz.orderbook_depth_history (symbol, interval, exchange_scope, range_pct, ts DESC);

COMMENT ON TABLE biz.orderbook_depth_history IS
  'CoinGlass 盘口深度历史：±range_pct 价格区间内的累计挂单 USD（静态盘口存量）。'
  '⚠️ 仅供标定/回测消费，禁止接入 scan_daemon 实时判定；'
  '⚠️ 与成交额（流量）不同量纲，严禁换算/相加；'
  '⚠️ 无 best bid/ask 字段 ⇒ 价差维度不可得；'
  '⚠️ 静态深度是可吃量的**上界**（异动瞬间撤单 ⇒ 真实可吃 ≤ 静态），标定结论只能当必要条件读。';

COMMENT ON COLUMN biz.orderbook_depth_history.symbol IS
  '本库合约码（BTCUSDT / 1000PEPEUSDT），与 biz.asset_klines.symbol 同口径。'
  '⚠️ 请求侧口径：单所端 ask-bids-history 直传合约码；聚合端 aggregated-ask-bids-history'
  ' 须先去 USDT 后缀映射为币种基码（BTC / 1000PEPE）再请求，落库仍写回合约码。'
  '（与 liquidation_history 同一映射教训：两个端点的 symbol 取值域不同）。';
COMMENT ON COLUMN biz.orderbook_depth_history.interval IS
  '粒度（HOBBYIST 地板 4h；1h ⇒ code=403 upgrade_required=STANDARD，HTTP 恒 200）。进 PK 禁混桶。';
COMMENT ON COLUMN biz.orderbook_depth_history.exchange_scope IS
  'binance = 单所（ask-bids-history, exchange=Binance，与 asset_klines 同所免跨所混算）；'
  'all = 跨所聚合（aggregated-ask-bids-history, exchange_list=ALL 或 supported-exchanges 全量）。'
  '进 PK ⇒ 两口径结构上不可能混算。本期只采 binance。';
COMMENT ON COLUMN biz.orderbook_depth_history.range_pct IS
  '深度档（官方取值 0.25/0.5/0.75/1/2/3/5/10，缺省=1 ⇒ ±1%）。'
  '⚠️ 不是档位序号，是**价格百分比带宽**；range=2 的深度 ⊃ range=1（累计区间更大）。进 PK 禁混桶。';
COMMENT ON COLUMN biz.orderbook_depth_history.ts IS
  '区间**起点**（UTC，与 interval 对齐）。⚠️ 最新一桶是**正在累积**的未完成区间，重跑即覆盖（PK + UPSERT）。';
COMMENT ON COLUMN biz.orderbook_depth_history.bids_usd IS
  '±range_pct 内累计**买单**挂单 USD（4h 桶内快照/聚合，服务端口径）。NULL = 未取到，**不得补 0**。'
  '⚠️ 与 biz.asset_klines.quote_vol（成交额）不同量纲：此为存量、彼为流量，严禁换算。';
COMMENT ON COLUMN biz.orderbook_depth_history.asks_usd IS
  '±range_pct 内累计**卖单**挂单 USD。NULL = 未取到，**不得补 0**。';

-- 保留策略：不纳入 prune_scan_data（回测资产；体量 ≈ 294 币 × 1080 点 × 档数 ≈ 31.7 万行/档，可控）。

-- 2. 回填游标（中断后 --resume 用；294 币 ≈12 min < 容器寿命，但保留续跑能力以防调度窗口冲突）
CREATE TABLE IF NOT EXISTS biz.ob_depth_backfill_cursor (
    symbol          TEXT        NOT NULL,
    interval        TEXT        NOT NULL,
    exchange_scope  TEXT        NOT NULL,
    range_pct       NUMERIC(5,2) NOT NULL,
    done_through    TIMESTAMPTZ NOT NULL,        -- 已成功覆盖到的区间起点（含）
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, interval, exchange_scope, range_pct)
);

COMMENT ON TABLE biz.ob_depth_backfill_cursor IS
  '盘口深度回填游标。--resume 依据本表 done_through 跳过已覆盖币。'
  '⚠️ 仅在**整币全区间成功**后推进（失败/截断不留游标，避免把半截数据当成已完成）。';
COMMENT ON COLUMN biz.ob_depth_backfill_cursor.done_through IS
  '已成功覆盖到的**区间起点（含）**，取值 = 本次实际落库的最早 ts（不是请求窗口起点——'
  '接口 @4h 只回 180 天，窗口更大时实际最早点会更晚）。';
