-- 为 AI 内容分类回填增加「分类理由 + 失败标记」字段，实现失败可追踪、可精确重试。
--
-- classify_reason：AI 分类成功时返回的简短理由。
-- classify_error ：AI 分类失败时的原因（AI 调用失败/解析失败/未匹配等），
--                   同时 classify_method 置为 'ai_failed'，与未处理项区分开。

ALTER TABLE biz.doc_source_entry
    ADD COLUMN IF NOT EXISTS classify_reason TEXT,
    ADD COLUMN IF NOT EXISTS classify_error  TEXT;

-- 失败项精确定位（重跑 --method ai_failed 时走此索引）
CREATE INDEX IF NOT EXISTS idx_doc_source_entry_classify_error
    ON biz.doc_source_entry (classify_method) WHERE classify_method = 'ai_failed';
