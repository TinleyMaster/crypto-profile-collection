-- ============================================================
-- fix_061_scan_signal_breakout.sql
-- 信号延续确认（设计文档 §6.3 的 `active → confirmed` 一段）：
--   biz.scan_signal 新增 breakout_px —— 「待突破价位」。
--
-- 背景（v0.5 记的「语义分歧」）：原 §3.2③ 把 trigger_price 记为「建议触发价
--   （待突破价位）」，但实现里 trigger_price = 触发周期最后一根 K 线收盘价
--   = 信号发出时的价格（入场位）。库里从来没有「待突破价位」这个量
--   ⇒ confirmed 判不出来。
--
-- 处置：把「待突破价位」**独立成列**，trigger_price 的入场位语义保持不变
--   （渲染层与执行层 `phase_execute_scan_signal` 都在消费它，不能改含义）。
--
-- 口径（生产者写入）：breakout_px = **触发根极值** —— p_dir='up' 取 bar.high_px、
--   'down' 取 bar.low_px，与 trigger_price 取**同一根**（该根多为未收盘条
--   ⇒ 极值是「截至信号时刻」的运行极值，无未来信息）。因 high ≥ close ≥ low，
--   该位恒落在入场价的正确一侧（做多在价上、做空在价下）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS。存量行保持 NULL（不参与确认、也不回填 ——
--   触发根极值无法从历史列重建，故不回填是正确选择而非偷懒）。
-- ============================================================

ALTER TABLE biz.scan_signal
    ADD COLUMN IF NOT EXISTS breakout_px NUMERIC(24,8);

COMMENT ON COLUMN biz.scan_signal.breakout_px IS
    '延续确认位：触发根极值（up→high_px / down→low_px，与 trigger_price 同根）。'
    'signal_ts 后 BREAKOUT_WINDOW_H(6h) 内出现「已收盘 1h 收盘价」越过此位 → status=confirmed；'
    'confirmed = 延续已被市场跟随，仍属可动作集合（消费侧筛 active+confirmed）。'
    'NULL = 未计算（存量行）或生产者无 K 线可用。';