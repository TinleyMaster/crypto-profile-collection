-- 为 biz.doc_source_entry 添加发布时间字段，用于资料时效性标注和 RAG 时间加权排序
-- 发布时间来源：HTML meta (article:published_time) / JSON-LD datePublished / Last-Modified HTTP 头
ALTER TABLE biz.doc_source_entry
    ADD COLUMN IF NOT EXISTS published_at DATE;

COMMENT ON COLUMN biz.doc_source_entry.published_at IS
    '文档发布/最后更新日期，从 HTML meta/JSON-LD/HTTP Last-Modified 提取，用于时效性排序和 RAG 加权';

CREATE INDEX IF NOT EXISTS idx_doc_source_entry_published_at
    ON biz.doc_source_entry (published_at DESC NULLS LAST);
