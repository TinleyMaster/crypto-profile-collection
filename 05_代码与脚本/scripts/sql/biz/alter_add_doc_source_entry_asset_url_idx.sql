-- 支持按 (entity_type, asset_id, entry_url) 精确查找已有入口，
-- 让 select_cmc_doc_source_candidates.sql 的「缺失 URL」判定走索引，
-- 避免对 doc_source_entry 全表扫描。
CREATE INDEX IF NOT EXISTS idx_doc_source_entry_asset_url
    ON biz.doc_source_entry (entity_type, asset_id, entry_url);
