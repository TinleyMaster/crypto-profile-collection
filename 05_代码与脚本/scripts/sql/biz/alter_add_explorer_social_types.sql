-- 扩展 doc_source_entry.entry_type 允许值：新增 explorer（区块浏览器/链上数据）
-- 与 social（社交媒体/社区）。配套 taxonomy.SOURCE_TYPES 与 classify_link 域名规则。
ALTER TABLE biz.doc_source_entry DROP CONSTRAINT IF EXISTS chk_doc_source_entry_type;

ALTER TABLE biz.doc_source_entry ADD CONSTRAINT chk_doc_source_entry_type
CHECK (entry_type IN (
    'official_website',
    'docs',
    'docs_portal',
    'whitepaper_page',
    'github',
    'medium',
    'announcement',
    'twitter',
    'telegram',
    'reddit',
    'facebook',
    'explorer',
    'social',
    'other'
));
