-- ============================================================
-- fix_050_scan_stall_dedup.sql
-- 统一「采集停摆告警」去重键，消除 daemon 内置告警与外部看门狗重复发信
--
-- 背景：2026-09-21 用户在同一停摆事件上收到 2 封邮件——
--   01:00 外部看门狗（task='watchdog_scan'）发 🔴 数据停摆
--   01:03 scan_daemon 内置检测（task='stall_alert'）发 ⚠️ 采集停摆
--   两条路径各自独立去重、互不知情 → 同一件事重复打扰
--
-- 处理：合并为单一共享 key 'scan_stall'，两路径共用去重状态与 6h 静默期。
-- ============================================================

-- 1. 合并历史告警状态到 scan_stall（取最新一次告警时间，避免合并后立即重发）
INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at)
SELECT 'scan_stall', MAX(last_email_ts), NOW()
FROM biz.scan_stall_alert
WHERE task IN ('stall_alert', 'watchdog_scan')
HAVING COUNT(*) > 0
ON CONFLICT (task) DO UPDATE
SET last_email_ts = GREATEST(
        COALESCE(biz.scan_stall_alert.last_email_ts, EXCLUDED.last_email_ts),
        COALESCE(EXCLUDED.last_email_ts, biz.scan_stall_alert.last_email_ts)),
    updated_at = NOW();

-- 2. 清理旧 key 行（去重状态已合并至 scan_stall）
DELETE FROM biz.scan_stall_alert WHERE task IN ('stall_alert', 'watchdog_scan');

COMMENT ON COLUMN biz.scan_stall_alert.task IS
    '共享去重键 scan_stall（scan_daemon 内置停摆告警 + 外部看门狗共用）';