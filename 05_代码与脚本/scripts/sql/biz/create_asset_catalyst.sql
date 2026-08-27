-- 币安新闻/公告催化剂表
-- 来源：币安 CMS 公开接口（bapi/composite/v1/public/cms/*）
-- 用途：事件层催化剂数据，关联到具体代币交易对

CREATE SCHEMA IF NOT EXISTS biz;

CREATE TABLE IF NOT EXISTS biz.asset_catalyst (
    catalyst_id         BIGSERIAL PRIMARY KEY,
    source_code         VARCHAR(32) NOT NULL,
    source_article_id   VARCHAR(128) NOT NULL,
    source_article_code VARCHAR(128),
    asset_id            BIGINT REFERENCES core.asset(asset_id) ON DELETE SET NULL,
    title               TEXT NOT NULL,
    body_text           TEXT,
    body_html           TEXT,
    published_at        TIMESTAMPTZ NOT NULL,
    event_category      VARCHAR(128),
    event_subcategory   VARCHAR(128),
    related_pairs       TEXT[],
    source_url          TEXT,
    seo_keywords        TEXT[],
    share_count         INTEGER DEFAULT 0,
    raw_json            JSONB,
    ai_processed        BOOLEAN DEFAULT FALSE,
    ai_event_type       VARCHAR(64),
    ai_sentiment        VARCHAR(16),
    ai_summary          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  biz.asset_catalyst IS '资产催化剂事件（币安新闻/公告等官方事件源）';
COMMENT ON COLUMN biz.asset_catalyst.catalyst_id IS '催化剂事件ID';
COMMENT ON COLUMN biz.asset_catalyst.source_code IS '来源编码：binance_news / binance_listing 等';
COMMENT ON COLUMN biz.asset_catalyst.source_article_id IS '源文章ID（币安 article id）';
COMMENT ON COLUMN biz.asset_catalyst.source_article_code IS '源文章code（币安 article code，用于查详情）';
COMMENT ON COLUMN biz.asset_catalyst.asset_id IS '关联资产ID（由related_pairs映射）';
COMMENT ON COLUMN biz.asset_catalyst.title IS '标题';
COMMENT ON COLUMN biz.asset_catalyst.body_text IS '正文纯文本（HTML清洗后）';
COMMENT ON COLUMN biz.asset_catalyst.body_html IS '正文原始HTML';
COMMENT ON COLUMN biz.asset_catalyst.published_at IS '发布时间';
COMMENT ON COLUMN biz.asset_catalyst.event_category IS '事件一级分类（币安栏目名）';
COMMENT ON COLUMN biz.asset_catalyst.event_subcategory IS '事件二级分类';
COMMENT ON COLUMN biz.asset_catalyst.related_pairs IS '关联交易对原始数组（如 ["BTCUSDT"]）';
COMMENT ON COLUMN biz.asset_catalyst.source_url IS '原文链接';
COMMENT ON COLUMN biz.asset_catalyst.seo_keywords IS 'SEO关键词';
COMMENT ON COLUMN biz.asset_catalyst.share_count IS '分享数（热度参考）';
COMMENT ON COLUMN biz.asset_catalyst.raw_json IS 'API返回原始JSON全量存档';
COMMENT ON COLUMN biz.asset_catalyst.ai_processed IS 'AI是否已处理（事件类型/情感/摘要）';
COMMENT ON COLUMN biz.asset_catalyst.ai_event_type IS 'AI提取的事件类型';
COMMENT ON COLUMN biz.asset_catalyst.ai_sentiment IS 'AI情感倾向：positive / neutral / negative';
COMMENT ON COLUMN biz.asset_catalyst.ai_summary IS 'AI摘要';
COMMENT ON COLUMN biz.asset_catalyst.created_at IS '创建时间';
COMMENT ON COLUMN biz.asset_catalyst.updated_at IS '更新时间';

-- 唯一约束：同一来源同一文章只存一条
ALTER TABLE biz.asset_catalyst
    DROP CONSTRAINT IF EXISTS uq_asset_catalyst_source_article;
ALTER TABLE biz.asset_catalyst
    ADD CONSTRAINT uq_asset_catalyst_source_article
    UNIQUE (source_code, source_article_id);

-- 索引
CREATE INDEX IF NOT EXISTS idx_asset_catalyst_asset_id
    ON biz.asset_catalyst (asset_id) WHERE asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_asset_catalyst_published_at
    ON biz.asset_catalyst (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_asset_catalyst_source_published
    ON biz.asset_catalyst (source_code, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_asset_catalyst_unprocessed
    ON biz.asset_catalyst (catalyst_id) WHERE NOT ai_processed;
