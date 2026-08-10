-- 为 doc_source_entry 添加 spa_crawled_at 字段，追踪 SPA 无头浏览器爬取的处理时间
-- 用于 B3 SPA 无头浏览器爬取任务的进度统计

ALTER TABLE biz.doc_source_entry
    ADD COLUMN IF NOT EXISTS spa_crawled_at timestamptz;

COMMENT ON COLUMN biz.doc_source_entry.spa_crawled_at IS 'SPA 无头浏览器爬取处理时间';