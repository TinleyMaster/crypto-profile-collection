-- ============================================================
-- fix_049_scan_freshness.sql
-- 盘面扫描·数据新鲜度护栏（防采集停摆时用陈旧数据出假信号）
--   1. biz.scan_stall_alert  采集停摆告警去重状态（单行）
--
-- 背景：2026-09-18 采集停摆 ~2h，主池扫描用 03:40-03:50 拉升快照在 05:44
--       算出一个"15m +2.85% / OI +3.9% 的 S1 做多"假信号，邮件发出时币已崩。
--       护栏在扫描侧跳过陈旧数据，此处记录"采集停摆"告警邮件的发送时间（去重）。
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.scan_stall_alert (
    task           TEXT PRIMARY KEY,      -- 固定 'stall_alert'
    last_email_ts  TIMESTAMPTZ,           -- 上次发送"采集停摆"告警的时间（重发去重）
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.scan_stall_alert IS '盘面扫描采集停摆告警去重状态（单行）';
