-- fix_073_funding_interval.sql
-- NEW-C（2026-09-27，盘面告警邮件审计 §三 NEW-C）：资金费率年化按品种实际结算间隔换算。
--
-- 背景：biz.asset_derivatives.funding_rate 存的是当期费率（小数），告警卡片此前按硬编码
--   ×3×365（= 8h 结算）换算年化。实测 Binance 存在 4h 结算品种（LSK/ZRO/PAXG/ASTER/
--   GRAM/LA/MOODENG 等）⇒ 对它们年化被**低估一半**（应为 ×6×365）。本封 9 币中 7 个受影响。
--
-- 本列由采集侧 phase_derivatives_batch.py 从结算历史（get_funding_rate_history）相邻
--   funding_time 差的中位数推导（取 OI 价值最大的交易所，回退任一可得者）；展示侧
--   scan_daemon.py 按 (24/间隔)×365 换算年化，列缺失/NULL 时回退 8h（保持旧口径，不劣化）。
--
-- 幂等：ADD COLUMN IF NOT EXISTS，可重复执行。
-- 注意：DDL 后须立即 commit（项目教训：未及时 commit 会持 AccessExclusiveLock 阻塞表读）。

ALTER TABLE biz.asset_derivatives
    ADD COLUMN IF NOT EXISTS funding_interval_h NUMERIC(4,1);

COMMENT ON COLUMN biz.asset_derivatives.funding_interval_h
    IS '资金费率结算间隔（小时）：由结算历史相邻 funding_time 差的中位数推导（Binance U 本位多为 8，部分品种 4/1）；NULL = 未推导出，消费侧回退 8h';