-- 为 SPA 爬取查询添加 needs_browser 部分索引
-- 查询: WHERE needs_browser = TRUE ORDER BY entry_id LIMIT N
-- 没有索引时只能全表扫描，表越大越慢

CREATE INDEX IF NOT EXISTS idx_dse_needs_browser
    ON biz.doc_source_entry (entry_id)
    WHERE needs_browser = TRUE;
