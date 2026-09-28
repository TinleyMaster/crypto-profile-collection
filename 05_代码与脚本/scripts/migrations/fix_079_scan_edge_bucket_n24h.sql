-- ============================================================
-- fix_079_scan_edge_bucket_n24h.sql
-- 告警邮件「开仓依据」分母修正（复验_告警邮件开仓依据_8702607_2026-09-28 §五 N-8702-A）
--   biz.scan_edge_bucket 补 1 列：n_24h —— 该桶 24h **非空**样本数（真实分母）。
--
-- 为什么必须落表：
--   桶的 `n` 是 **1h** 口径（`aligned_ret_1h` 非空计数），而 fix_078 的
--   `sl_rate / ret_p75 / mfe_p75` 全在 **24h 子集**上算。邮件读 `n` 印「同档 n=57」，
--   而 24h 分位/止损率的真分母只有一半（实测 09-27 funding_sign/>0：n=57、24h=28）。
--   渲染层无法自证分母 ⇒ 必须由生产者把 24h 分母一并产出，渲染侧只印表里的数
--   （遵循 §四 铁律：模板不得内置任何统计数字/分母）。
--
--   列语义：
--     n_24h = 该桶 aligned_ret_24h 非空的行数（= fix_078 三列的真实分母）。
--   可空（旧行未回填时为 NULL）；消费侧 NULL 时回退 1h `n`（不劣化）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS + COMMENT，无数据回填、无破坏性操作。
--       （回填由 `build_scan_edge_report.py --date <日>` 重跑覆盖，非 SQL 职责。）
-- ============================================================

ALTER TABLE biz.scan_edge_bucket
    ADD COLUMN IF NOT EXISTS n_24h INTEGER;

COMMENT ON COLUMN biz.scan_edge_bucket.n_24h IS
    '该桶 24h 非空样本数（sl_rate/ret_p75/mfe_p75 的真实分母）；NULL = 旧行未回填（消费侧回退 1h n）';
