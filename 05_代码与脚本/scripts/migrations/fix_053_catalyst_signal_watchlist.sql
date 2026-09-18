-- =====================================================================
-- 催化剂信号分层：观察池（watch）状态（d3）
-- 编号：fix_053
-- 日期：2026-09-18
--
-- 背景（实测依据，biz.catalyst_outcome 按 resonance_state 分组，
--       收益从信号生成时刻起算的前瞻超额）：
--   confirmed（价格已同向反应≥5% 且放量）：72h 超额 -2.16%（n=47，方向命中 19.1%）
--   weak     （价格温和/未充分反应）    ：72h 超额 +1.58%（n=231）
--   divergent（方向背离）              ：72h 超额 +5.92%（n=12）
--   pending  （未反应）                ：72h 超额 +0.14%（n=167）
--   → 现有「等价格确认(confirmed)再开单」的闸门实为追高，前瞻为负。
--
-- 新的动作闸门（status 语义反转；composite_score/tier 口径不变，仍是单点真源）：
--   confirmed（已定价）  → watch  （观察池，不推送，等回撤/再定价）
--   weak     （未定价）  → open   （可动作）
--   divergent（方向背离）→ invalid（剔除）
--   pending  （未反应）  → watch  （观察）
--
-- 存量行收敛：不在此处批量 UPDATE。signal.upsert_to_db 的状态迁移规则为
--   「expired/done 终态冻结，其余行一律跟随当前 resonance_state」，
--   存量 open/confirmed 行会在下一轮 G6 重算时自动归入 watch。
--   如需立即收敛，可手工执行：
--     UPDATE biz.catalyst_signal SET status = CASE resonance_state
--       WHEN 'confirmed' THEN 'watch' WHEN 'divergent' THEN 'invalid'
--       WHEN 'weak' THEN 'open' WHEN 'pending' THEN 'watch' ELSE status END,
--       updated_at = NOW()
--     WHERE status IN ('open','watch','invalid') AND resonance_state IS NOT NULL;
--
-- 幂等性：DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT，可重复执行。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. status 枚举新增 'watch'
-- ---------------------------------------------------------------------
ALTER TABLE biz.catalyst_signal
    DROP CONSTRAINT IF EXISTS catalyst_signal_status_check;

ALTER TABLE biz.catalyst_signal
    ADD CONSTRAINT catalyst_signal_status_check
    CHECK (status IN ('open', 'watch', 'invalid', 'expired', 'done'));

COMMENT ON COLUMN biz.catalyst_signal.status IS
    '信号状态：open(可动作，价格未充分定价) / watch(观察池，价格已定价或待确认，不推送) / invalid(方向背离剔除) / expired(过期) / done(已了结)';

-- ---------------------------------------------------------------------
-- 2. 观察池索引
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_catalyst_signal_watch
    ON biz.catalyst_signal (status, created_at DESC) WHERE status = 'watch';