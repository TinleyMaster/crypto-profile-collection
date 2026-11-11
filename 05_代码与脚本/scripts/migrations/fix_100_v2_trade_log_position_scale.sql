-- fix_100_v2_trade_log_position_scale.sql
-- A 档按 chg24 分档调仓的落库留痕（2026-11-11）
-- 背景：A 档（SHORT_LONG）chg24 ∈ [50%, 75%) 为纯亏损池，实盘改为半仓名义
--       （60U→30U，杠杆 3x 不变）；chg24 ≥ 75% 维持满仓。
--       回测：半仓后 A 档累计 −319pp→−53pp、最大回撤 −933pp→−517pp，75%+ 右尾完全保留。
-- 用途：实盘按 position_scale 归因/对账（0.5 = 半仓，1.0 = 满仓；历史行为 NULL）。
-- 幂等：ADD COLUMN IF NOT EXISTS，可重复执行；新增列可空，不影响存量行。
ALTER TABLE biz.v2_trade_log
    ADD COLUMN IF NOT EXISTS position_scale NUMERIC(6,4);
