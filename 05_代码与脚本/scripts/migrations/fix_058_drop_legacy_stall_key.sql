-- ============================================================
-- fix_058_drop_legacy_stall_key.sql
-- 清理旧去重键残留行 + 清掉一次部署窗口误报的静默时间戳
--（审计_盘面扫描告警邮件_2026-09-21 · P0-3 收尾）
--
-- 背景：fix_050 统一去重键后，biz.scan_stall_alert 仍残留一行
--   task='stall_alert'  last_email_ts=2026-09-21 01:11:43 UTC
-- 该行写于 01:11:43，晚于统一键提交 25ad175（01:09 UTC），而新代码全库
-- 已无 'stall_alert' / 'watchdog_scan' 这两个键（grep 已确认）——即该行
-- 是「旧码实例仍存活」写入的。容器在 03:30:39 整体重启后旧实例已消亡
-- （/proc 仅 1 个 scan_daemon，supervisorctl uptime 全部 0:00:30），
-- 该行成为无人读写的死数据。
--
-- ⚠️ 刻意跳过 fix_050 的第 1 步（合并 MAX(last_email_ts) 到 scan_stall）：
--   scan_stall 当前 last_email_ts = NULL，是看门狗 03:20 走「已恢复」路径
--   清空的**正确状态**。若照抄 fix_050 合并，会把静默窗口重置到
--   01:11:43 + 6h = 07:11 UTC，导致这段时间内的真实停摆被去重吞掉
--   （正是审计 P0-2 所批评的告警失守）。故此处只删除、不合并。
-- ============================================================

DELETE FROM biz.scan_stall_alert WHERE task IN ('stall_alert', 'watchdog_scan');

-- 清掉 03:33:41 那次「部署窗口误报」留下的静默时间戳。
-- 起因：容器 03:30:39 重启后，scan_oi_cvd(offset 120s)/scan_alert(offset 180s)
-- 首轮尚未跑完即无心跳，被心跳判据当成「线程从未启动」→ 误发一封停摆告警。
-- 该误报会把真实停摆的告警压到 09:33 之后（6h 静默期），故必须清除。
-- 代码侧已修：新增 DAEMON_START_TASK 进程启动标记 + 首轮宽限。
-- 用精确时间戳定位，保证重复执行为幂等 no-op（不会误清真实告警）。
UPDATE biz.scan_stall_alert SET last_email_ts = NULL, updated_at = NOW()
WHERE task = 'scan_stall'
  AND last_email_ts = TIMESTAMPTZ '2026-09-21 03:33:41.009760+00';