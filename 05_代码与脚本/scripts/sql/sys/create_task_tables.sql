-- sys.task 表：后台任务状态持久化
-- 替代原来的 task_state/tasks.json 文件，支持跨服务共享状态
-- 调度器和 Flask 主应用都读写这张表

CREATE TABLE IF NOT EXISTS sys.task (
    task_id         VARCHAR(32) PRIMARY KEY,
    name            VARCHAR(512) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending / running / done / failed / stopped
    cmd             TEXT[] NOT NULL DEFAULT '{}',           -- 命令行参数数组
    started_at      TIMESTAMPTZ,                            -- 提交时间（pending 也有）
    ended_at        TIMESTAMPTZ,
    stats           JSONB NOT NULL DEFAULT '{}'::jsonb,     -- 进度、结果等统计数据
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_status ON sys.task(status);
CREATE INDEX IF NOT EXISTS idx_task_started_at ON sys.task(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_name ON sys.task(name);

-- sys.task_log 表：任务日志逐行存储
-- 替代原来的 task_state/logs/{task_id}.log 文件

CREATE TABLE IF NOT EXISTS sys.task_log (
    log_id          BIGSERIAL PRIMARY KEY,
    task_id         VARCHAR(32) NOT NULL REFERENCES sys.task(task_id) ON DELETE CASCADE,
    line_no         INTEGER NOT NULL,
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_log_task_id ON sys.task_log(task_id, line_no);
