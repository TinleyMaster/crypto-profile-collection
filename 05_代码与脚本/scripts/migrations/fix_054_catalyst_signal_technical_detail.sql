-- =====================================================================
-- 催化剂信号：G5 技术面明细落库（technical_detail）
-- 编号：fix_054
-- 日期：2026-09-21
--
-- 背景：
--   A 级 Alert 邮件需要展示「整个催化剂的决策过程」。G3/G4 的明细已落库
--   （persistence / fundamental_detail），只有 G5 技术面目前仅存
--   technical_state / entry_trigger / entry_price / stop_loss / take_profit
--   五个结果字段，MA5/MA20/MA60、30d 高低点、ATR 等推导依据全部丢失，
--   邮件里无法解释「档位是怎么算出来的」。
--
-- 口径：
--   technical_detail 存 TechnicalAnalyzer.analyze() 的 result.detail 原样 JSON，
--   字段固定为：
--     last_price / ma5 / ma20 / ma60 / high_30d / low_30d / atr_30d
--     / state / impact_direction / stop_loss / take_profit / rr_ratio
--   （见 workbench/catalyst/technical.py 的 detail 组装处）
--
-- 幂等性：ADD COLUMN IF NOT EXISTS + COMMENT，可重复执行。
--
-- 存量回填：不在此处批量 UPDATE（历史技术位已随行情漂移，用当前 MA/ATR
--   冒充决策时点数据会造成新的失真）。用管道的一次性回填命令：
--     python bin/phase_catalyst_pipeline.py --backfill-technical-detail
--   实现（backfill_technical_detail，只写 technical_detail 一列，不重算
--   persistence/档位/tier/status，因此不会改写历史决策结果）：
--     - status IN ('open','watch') 且 technical_detail IS NULL 的行被填充；
--     - status IN ('expired','done') 的行保持 NULL（不回填，避免口径污染）。
--
--   不进 run_slow_g3g5 候选查询的原因：该查询的 gap 过滤语义是「G3-G5 缺失」，
--   把「明细缺失」并进去会让下一轮慢通道一次性重算全部 open/watch 信号的
--   档位与档级（数千行），属非预期副作用。
-- =====================================================================

ALTER TABLE biz.catalyst_signal
    ADD COLUMN IF NOT EXISTS technical_detail JSONB;

COMMENT ON COLUMN biz.catalyst_signal.technical_detail IS
    'G5 技术面明细 JSON：last_price/ma5/ma20/ma60/high_30d/low_30d/atr_30d/state/impact_direction/stop_loss/take_profit/rr_ratio；供决策链追溯与 A 级 Alert 邮件展开使用';