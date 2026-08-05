-- 为 biz.doc_source_entry 添加 AI 噪声检查时间戳字段
-- 已通过 AI 判断为"投研相关"的条目会被标记，下次任务跳过，避免重复消耗 AI 配额

ALTER TABLE biz.doc_source_entry
    ADD COLUMN IF NOT EXISTS ai_noise_checked_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN biz.doc_source_entry.ai_noise_checked_at IS
    'AI 噪声检查时间戳：NULL=未检查，有值=已判定为投研相关（无需再查）。噪声条目直接删除，不留标记。';

-- 为后续查询加速
CREATE INDEX IF NOT EXISTS idx_doc_source_entry_ai_noise_checked
    ON biz.doc_source_entry (ai_noise_checked_at)
    WHERE ai_noise_checked_at IS NULL;
