-- 扩展 doc_source_entry.entry_type 允许值，完整类型列表
ALTER TABLE biz.doc_source_entry DROP CONSTRAINT IF EXISTS chk_doc_source_entry_type;

ALTER TABLE biz.doc_source_entry ADD CONSTRAINT chk_doc_source_entry_type
CHECK (entry_type IN (
    'official_website',
    'docs',
    'docs_portal',
    'github',
    'medium',
    'announcement',
    'twitter',
    'telegram',
    'reddit',
    'facebook',
    'other'
));
