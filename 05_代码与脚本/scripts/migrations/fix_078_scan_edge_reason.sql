-- ============================================================
-- fix_078_scan_edge_reason.sql
-- 告警邮件「开仓依据」栏（审计_告警邮件开仓依据缺失_2026-09-28 §三/§四）· schema 支撑
--   1) biz.scan_edge_bucket 补 3 列：sl_rate / ret_p75 / mfe_p75
--   2) dim 新增两个分类维：funding_sign（<=0 / >0）、cvd_align（同向 / 反向·缺失）
--
-- 为什么必须落在表里（审计 §四 铁律）：
--   邮件模板**不得内置任何统计数字**（+6.57% / P75 之类）—— 市场一变它就变成谎言，
--   且无人会发现（本项目高频复发项：派生字段与单一真源失联）。故把「条件期望」交给
--   `build_scan_edge_report.py` 的 BUCKETS 扩维后直接产出到本表，渲染侧只做
--   「按本卡数值查桶 + 印表里的数」。
--
--   列语义：
--     sl_rate  = 该桶 24h 止损命中率（sl_hit_24h 占比）—— 依据行的风险披露项；
--     ret_p75  = 该桶 24h 方向对齐净收益 P75 —— 量比档「该档 24h P75」的取值来源；
--     mfe_p75  = 该桶 24h 最大有利偏移 P75 —— 「参考目标位」的取值来源。
--   三列均可空（桶样本为空/该字段全缺时为 NULL），渲染侧 NULL 即不出该依据行。
--
-- 幂等：全部 ADD COLUMN IF NOT EXISTS + COMMENT，无数据回填、无破坏性操作。
--       （回填由 `build_scan_edge_report.py --date <日>` 重跑覆盖，非 SQL 职责。）
-- ============================================================

ALTER TABLE biz.scan_edge_bucket
    ADD COLUMN IF NOT EXISTS sl_rate  NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS ret_p75  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS mfe_p75  NUMERIC(10,4);

COMMENT ON COLUMN biz.scan_edge_bucket.sl_rate IS
    '该桶 24h 止损命中率（sl_hit_24h 占比）；NULL = 该桶无已结算止损样本';
COMMENT ON COLUMN biz.scan_edge_bucket.ret_p75 IS
    '该桶 24h 方向对齐净收益 P75（%）；「量比档 24h P75」依据行的取值来源';
COMMENT ON COLUMN biz.scan_edge_bucket.mfe_p75 IS
    '该桶 24h 最大有利偏移 P75（%）；「参考目标位」的取值来源（≠ 保证能到）';

-- dim 取值更新（新增两维；原 8 维不变）
COMMENT ON COLUMN biz.scan_edge_bucket.dim IS
    'vol_ratio | price_chg | oi_chg | timeframe | scenario | regime | confidence | pool '
    '| funding_sign | cvd_align（后两维见 fix_078：开仓依据的条件期望）';
