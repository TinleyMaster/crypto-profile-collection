-- fix_023: sys.task 加 category 列，支持按类别隔离执行槽
-- 背景：链上重任务（holder_snapshot/transfer_monitor）占满执行槽，饿死催化剂等核心任务
-- 策略：chain 类最多占1槽，core 类保底1槽，monitor 类最多1槽

ALTER TABLE sys.task ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'core';

-- 给存量 running 任务补 category（按 name 关键词自动分类）
UPDATE sys.task SET category = 'chain'
WHERE status = 'running'
  AND (name ILIKE '%holder_snapshot%' OR name ILIKE '%transfer_monitor%');

UPDATE sys.task SET category = 'monitor'
WHERE status = 'running'
  AND name ILIKE '%monitor%'
  AND category = 'core';

-- 索引：加速 runner_loop 按 category + status 查询
CREATE INDEX IF NOT EXISTS idx_task_category_status ON sys.task(category, status);
