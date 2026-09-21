-- fix_057: 盘面扫描数据来源标识 + 守护进程任务心跳
--
-- 背景（见 审计_盘面扫描告警邮件_2026-09-21.md 的 P0-2）：
--   1. biz.oi_cvd_snapshot 混入两种来源（5m 实时采样 / 1h 历史回填），同表同 exchange
--      无来源标识 → 任何基于 MAX(ts) 的新鲜度判断都会被回填的 1h 行误导；且回填的 1h 行
--      会污染主池「OI 近 2 桶变化」的计算（5m 桶序列里混入 1h 桶）。
--   2. 「采集线程存活、扫描线程卡死」这一类故障此前完全无感——原停摆检测只看数据
--      MAX(ts)，不看任务是否真的在跑。新增 biz.scan_heartbeat 记录每轮心跳（无论是否
--      产出信号都写），使停摆检测不再依赖「有没有信号产出」这一间接指标。
--
-- 幂等：全部 IF NOT EXISTS，可重复执行。

ALTER TABLE biz.oi_cvd_snapshot
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'realtime';

COMMENT ON COLUMN biz.oi_cvd_snapshot.source IS
    '数据来源：realtime=5m 实时采样 / backfill=1h 历史回填 / ws=CVD 流式采集。'
    '新鲜度判断与主池 OI 变化计算只看 realtime，避免被回填行误导。';

CREATE INDEX IF NOT EXISTS idx_oi_cvd_snapshot_source_ts
    ON biz.oi_cvd_snapshot (source, ts DESC);

CREATE TABLE IF NOT EXISTS biz.scan_heartbeat (
    task         TEXT PRIMARY KEY,
    last_run_at  TIMESTAMPTZ NOT NULL,
    last_ok_at   TIMESTAMPTZ,
    last_error   TEXT,
    round_count  BIGINT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE biz.scan_heartbeat IS
    'scan_daemon 各任务每轮心跳（成功/异常都写），用于检测「采集正常但扫描停产」。';