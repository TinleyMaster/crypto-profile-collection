-- 为统一投研链接分类增加「内容主题」维度字段，并修复 entry_type CHECK 约束。
--
-- 背景：历史上代码里大量使用 whitepaper_page 作为来源类型，但数据库 CHECK
-- 约束从未允许该值，导致这类链接始终无法正确落库。此处一并修复。

-- 1) doc_source_entry：新增内容主题多标签 + 分类方法与置信度
ALTER TABLE biz.doc_source_entry
    ADD COLUMN IF NOT EXISTS content_topics      TEXT[],
    ADD COLUMN IF NOT EXISTS classify_method     TEXT,
    ADD COLUMN IF NOT EXISTS classify_confidence REAL;

-- 2) 修复 doc_source_entry.entry_type 约束：补上 whitepaper_page
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
    'other'
));

-- 3) doc_asset：新增内容主题多标签（保留原有 doc_type 兼容）
ALTER TABLE biz.doc_asset
    ADD COLUMN IF NOT EXISTS content_topics      TEXT[],
    ADD COLUMN IF NOT EXISTS classify_method     TEXT,
    ADD COLUMN IF NOT EXISTS classify_confidence REAL;

-- 4) research_url：新增内容主题多标签（该表当前为空，为后续 NotebookLM 分类预留）
ALTER TABLE biz.research_url
    ADD COLUMN IF NOT EXISTS content_topics      TEXT[],
    ADD COLUMN IF NOT EXISTS classify_method     TEXT,
    ADD COLUMN IF NOT EXISTS classify_confidence REAL;

-- 索引：按分类方法/置信度筛选（供后续 AI 补分类低置信度项）
CREATE INDEX IF NOT EXISTS idx_doc_source_entry_classify
    ON biz.doc_source_entry (classify_method, classify_confidence);

-- 5) content_topics 数组 GIN 索引（供一键投研缺失清单按主题精确判定）
CREATE INDEX IF NOT EXISTS idx_doc_source_entry_content_topics
    ON biz.doc_source_entry USING GIN (content_topics);
CREATE INDEX IF NOT EXISTS idx_doc_asset_content_topics
    ON biz.doc_asset USING GIN (content_topics);
CREATE INDEX IF NOT EXISTS idx_research_url_content_topics
    ON biz.research_url USING GIN (content_topics);
