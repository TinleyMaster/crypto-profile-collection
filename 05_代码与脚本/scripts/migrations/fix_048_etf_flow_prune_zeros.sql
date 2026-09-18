-- fix_048: 清理 biz.etf_flow_daily 历史 0 值占位行（2026-09-18 审计 F4）
--
-- 背景：上游 cryptoetf.today 对「窗口内但当日数据尚未发布」的日期返回 netFlowUsdM=0
-- 作为占位（非真实净流）。历史 ingest 将其当真值写入，且增量只认 MAX(flow_date)，
-- 导致 0 值占位行永久固化（13 symbol 过半记录为伪 0，自 2026-08-09 起）。
--
-- 用法（先看将清多少行，再执行）：
--   1) 部署含 F1/F2 的新版 ingest_cryptoetf_flow.py（0 值不再入库、增量回补缺失日期）
--   2) 预览： SELECT COUNT(*) FROM biz.etf_flow_daily
--              WHERE source_code='cryptoetf' AND net_flow_usd_m = 0 AND flow_date < CURRENT_DATE;
--   3) 本迁移：删除历史 0 值占位（保留当日可能的占位，日期窗口保护）
--   4) 重拉真实数据：python ingest_cryptoetf_flow.py --full
--      （或 python ingest_cryptoetf_flow.py --backfill-days 45）
--
-- 等价命令：python ingest_cryptoetf_flow.py --prune-zeros --dry-run / 去掉 --dry-run
-- 本迁移为一次性数据清洗，可安全重复执行（第二次删 0 行）。

DELETE FROM biz.etf_flow_daily
WHERE source_code = 'cryptoetf'
  AND net_flow_usd_m = 0
  AND flow_date < CURRENT_DATE;