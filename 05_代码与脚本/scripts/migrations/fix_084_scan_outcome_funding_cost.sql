-- fix_084: biz.scan_signal_outcome 补资金费率成本对照列（已知缺口 §12.1-A4）
--
-- 背景（2026-10-02）：
--   结算与回测的成本口径一直**只有双边 taker 0.1%**，不含滑点、**不含资金费率**。
--   而信号语义是「24h 持有观察」，24h 内跨多个资金费率结算点 ⇒ 费率是真实持仓成本
--   却被记为 0，24h 净期望被系统性高估。当前「P↑OI↑ 线上 +0.04%」「A3 各场景零点附近」
--   这类接近 0 的读数，其可信度完全取决于是否扣了费率。
--
-- 本轮范围（用户拍板）：
--   * **只补资金费率**。滑点不做——SCAN-LIQ-DEPTH-001 阶段C 已实现盘口深度线性滑点模型
--     （backtest_liq_layer.py），结论 X≤1,000 USDT 时侵蚀 <0.04pp 可忽略。
--   * **只新增列，既有列语义与数值一律不变**（aligned_ret_* / btc_ret_* / excess_* 原样保留）
--     ⇒ 新旧口径可直接对照、下游零破坏。
--   * **日报/邮件口径暂不切换**（不改 scan_edge_daily 与日报渲染的可见数字）。
--
-- 口径（写入侧 collect_scan_outcome.py 实现，此处仅登记列语义）：
--   * 区间边界 **左开右闭 (entry_ts, entry_ts + w]**：funding_time 结算的是该时点**之前**
--     那一持仓期的费用，恰好落在 entry_ts 的结算点属入场前，不计入。
--   * 方向符号：多头（p_dir='up'）正费率**支付**、负费率**收取**；空头相反。
--     funding_pct = sign × Σ(区间内结算点 rate) × 100，sign = +1 / −1。
--   * net_ret_w = aligned_ret_w − funding_pct_w（aligned_ret 已扣 0.1% 手续费，此处叠加）。
--   * BTC beta 对照侧同口径扣费（用 symbol='BTCUSDT' 的费率序列），否则 excess 系统性偏移。
--   * **缺数据不得记 0**：区间内无结算点但该 symbol 有费率覆盖 → 合法 0；
--     **完全无覆盖** → NULL 并以 funding_src 标记，不参与统计。
--   * ⚠️ 结算周期**不统一**（2026-10-02 实测全市：4h 73,994 条主导 / 8h 32,796 / 1h 2,891）
--     ⇒ **不得硬编码「24h = 3 个结算点」**，一律按区间内真实结算点求和。
--
-- 幂等：全部 ADD COLUMN IF NOT EXISTS，可重复执行；纯 DDL 无 DML。
-- 应用：python 05_代码与脚本/scripts/apply_migration.py fix_084_scan_outcome_funding_cost.sql
-- 锁：本文件只有 ADD COLUMN（PG11+ 加列默认值走元数据、不重写表），但 DDL 仍会取
--     AccessExclusiveLock ⇒ **执行后立即 commit**，避免阻塞 biz.scan_signal_outcome 读取。
-- ⚠️ 本迁移必须先于 collect_scan_outcome.py 上线：该脚本的 upsert 列清单由 OUT_COLS 驱动，
--     列不存在会 UndefinedColumn。

-- ============================================================
-- 0) 执行前基线（**首次运行**应读到 0 列；复跑仍为 17 列）
-- ============================================================
SELECT '新列数(应为 17)' AS cls, count(*) AS n
FROM information_schema.columns
WHERE table_schema = 'biz' AND table_name = 'scan_signal_outcome'
  AND column_name IN (
      'funding_src',
      'funding_pct_1h','funding_pct_4h','funding_pct_12h','funding_pct_24h',
      'net_ret_1h','net_ret_4h','net_ret_12h','net_ret_24h',
      'btc_net_ret_1h','btc_net_ret_4h','btc_net_ret_12h','btc_net_ret_24h',
      'excess_net_1h','excess_net_4h','excess_net_12h','excess_net_24h');

-- ============================================================
-- 1) 区间资金费率成本（%，已按多/空对齐符号）
-- ============================================================
ALTER TABLE biz.scan_signal_outcome
    ADD COLUMN IF NOT EXISTS funding_pct_1h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS funding_pct_4h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS funding_pct_12h NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS funding_pct_24h NUMERIC(10,4);

-- ============================================================
-- 2) 含费率净收益（%= aligned_ret_* − funding_pct_*）
-- ============================================================
ALTER TABLE biz.scan_signal_outcome
    ADD COLUMN IF NOT EXISTS net_ret_1h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS net_ret_4h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS net_ret_12h NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS net_ret_24h NUMERIC(10,4);

-- ============================================================
-- 3) BTC 对照侧含费率净收益（同方向同窗口，用 BTCUSDT 费率序列）
-- ============================================================
ALTER TABLE biz.scan_signal_outcome
    ADD COLUMN IF NOT EXISTS btc_net_ret_1h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS btc_net_ret_4h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS btc_net_ret_12h NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS btc_net_ret_24h NUMERIC(10,4);

-- ============================================================
-- 4) 含费率超额（新口径 alpha = net_ret_* − btc_net_ret_*）
-- ============================================================
ALTER TABLE biz.scan_signal_outcome
    ADD COLUMN IF NOT EXISTS excess_net_1h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS excess_net_4h  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS excess_net_12h NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS excess_net_24h NUMERIC(10,4);

-- ============================================================
-- 5) 费率覆盖状态（ok=已到期窗口全覆盖 / partial=部分 / none=全无覆盖；NULL=尚无到期窗口）
-- ============================================================
ALTER TABLE biz.scan_signal_outcome
    ADD COLUMN IF NOT EXISTS funding_src TEXT;

COMMENT ON COLUMN biz.scan_signal_outcome.funding_pct_24h IS
    '24h 持有区间 (alerted_at, alerted_at+24h] 内资金费率结算点之和（%，多付空收已对齐符号）；无覆盖为 NULL';
COMMENT ON COLUMN biz.scan_signal_outcome.net_ret_24h IS
    '含费率净收益% = aligned_ret_24h − funding_pct_24h（§12.1-A4 新口径；日报暂未切换）';
COMMENT ON COLUMN biz.scan_signal_outcome.btc_net_ret_24h IS
    'BTC 同方向同窗口含费率净收益%（BTCUSDT 费率序列），excess_net 的对照基准';
COMMENT ON COLUMN biz.scan_signal_outcome.excess_net_24h IS
    '含费率超额% = net_ret_24h − btc_net_ret_24h（新口径 alpha）';
COMMENT ON COLUMN biz.scan_signal_outcome.funding_src IS
    '费率覆盖三态：ok 全到期窗口有覆盖 / partial 部分 / none 全无覆盖（缺数据不记 0）/ NULL 尚无到期窗口';

-- ============================================================
-- 6) 执行后校验（应读到 17 列）
-- ============================================================
SELECT '新列数(应为 17)' AS cls, count(*) AS n
FROM information_schema.columns
WHERE table_schema = 'biz' AND table_name = 'scan_signal_outcome'
  AND column_name IN (
      'funding_src',
      'funding_pct_1h','funding_pct_4h','funding_pct_12h','funding_pct_24h',
      'net_ret_1h','net_ret_4h','net_ret_12h','net_ret_24h',
      'btc_net_ret_1h','btc_net_ret_4h','btc_net_ret_12h','btc_net_ret_24h',
      'excess_net_1h','excess_net_4h','excess_net_12h','excess_net_24h');