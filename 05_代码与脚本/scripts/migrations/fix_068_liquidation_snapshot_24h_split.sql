-- ============================================================
-- fix_068_liquidation_snapshot_24h_split.sql
-- biz.liquidation_snapshot 补齐「滚动 24h 多空分列」（幂等，可重复执行）
--   1. long_liq_usd_24h  NUMERIC(24,2)
--   2. short_liq_usd_24h NUMERIC(24,2)
-- 设计依据：04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md §5 第 3 段（P0-D 方向行依赖）
-- 写入端：scripts/bin/scan_daemon.py 的 task_scan_liquidation（INSERT ... ON CONFLICT DO UPDATE）
-- 消费端：workbench/macro_market.py 的 fetch_liquidation_overview（只读）→ 早报「3衍生品」展示，不进分
--
-- ⚠️ 口径（三处必须一致：列注释 / 代码注释 / 邮件脚注）
--   · 全交易所口径（CoinGlass coin-list），**滚动 24h 窗口快照**，非「某区间累计」；
--   · 与 liq_usd_24h 属同族滚动窗口 ⇒ 可比较占比（如「近 1h 占 24h 的 X%」），
--     **严禁跨桶差分**（相邻快照相减 = 新滚入 − 滚出，平稳时≈0、回落时为负）；
--     **严禁**与 biz.liquidation_history 的 4h 分段增量换位/相加（不同口径不可换算）。
--   · 历史行无法回补（滚动窗口值只存在于当时的响应里）⇒ 旧行保持 NULL，**不得补 0**。
--
-- 注：本迁移**只**补这两列；P1 的 biz.liquidation_history / biz.liquidation_backfill_cursor
--     属未开工项，另行编号，不在本次范围。
-- 应用方式：同既有 fix_* 迁移（本文件全部语句幂等，连跑两次第二次应为 0 变更、无异常）
-- ============================================================

ALTER TABLE biz.liquidation_snapshot
    ADD COLUMN IF NOT EXISTS long_liq_usd_24h  NUMERIC(24,2),
    ADD COLUMN IF NOT EXISTS short_liq_usd_24h NUMERIC(24,2);

COMMENT ON COLUMN biz.liquidation_snapshot.long_liq_usd_24h IS
  '滚动 24h 多单爆仓额（CoinGlass 全交易所口径）。与 liq_usd_24h 同族滚动窗口，'
  '可比较占比；严禁跨桶差分，严禁与 biz.liquidation_history 的 4h 分段增量换算。'
  '历史行 NULL，不得补 0。';

COMMENT ON COLUMN biz.liquidation_snapshot.short_liq_usd_24h IS
  '滚动 24h 空单爆仓额（CoinGlass 全交易所口径）。与 liq_usd_24h 同族滚动窗口，'
  '可比较占比；严禁跨桶差分，严禁与 biz.liquidation_history 的 4h 分段增量换算。'
  '历史行 NULL，不得补 0。';