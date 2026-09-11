-- sys.async_task_state 表：轻量级异步任务状态持久化
-- 替代原来的 task_state/*.json 文件方案（recrawl_state / unlock_state / kol_crawl_state 等）
-- 特点：
--   - 按 (task_type, entity_key) 唯一标识一个异步任务
--   - payload 为 JSONB，存任意结果/进度数据
--   - 支持行级锁（SELECT ... FOR UPDATE）替代文件锁
--   - 保留历史记录（不自动删除），便于追溯

CREATE TABLE IF NOT EXISTS sys.async_task_state (
    task_type       VARCHAR(50) NOT NULL,    -- 任务类型：recrawl / kol_crawl / ...
    entity_key      VARCHAR(200) NOT NULL,   -- 实体标识：asset_id / profile_id / 自定义 key
    status          VARCHAR(20) NOT NULL DEFAULT 'idle',  -- idle / running / done / failed
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 任务结果/进度/元数据
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_type, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_async_task_state_status ON sys.async_task_state(status);
CREATE INDEX IF NOT EXISTS idx_async_task_state_task_type ON sys.async_task_state(task_type);
