-- FIX-013: 催化剂表扩展 — content_hash 跨源去重 + 多资产关联 + 多源合并
-- 日期：2026-08-26
-- 背景：接入 Binance Square News 后，同一条新闻会从 CMS 和 Square 两边来（ID 不同），
--       需要 content_hash 做跨源去重；同时一篇文章可能关联多个资产，
--       原 asset_id 单字段不够，需扩展为多资产关联表。

-- ============================================================
-- 第一步：加 content_hash 字段（跨源去重键）
-- ============================================================

ALTER TABLE biz.asset_catalyst
    ADD COLUMN IF NOT EXISTS content_hash CHAR(64);

COMMENT ON COLUMN biz.asset_catalyst.content_hash
    IS '内容哈希（sha256 of 归一化title+正文前200字），用于跨源去重';

-- 唯一索引：同内容只存一条（允许多来源合并）
CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_catalyst_content_hash
    ON biz.asset_catalyst (content_hash)
    WHERE content_hash IS NOT NULL;

-- 加速按 hash 查找
CREATE INDEX IF NOT EXISTS idx_asset_catalyst_content_hash
    ON biz.asset_catalyst (content_hash)
    WHERE content_hash IS NOT NULL;

-- ============================================================
-- 第二步：多资产关联表（一篇催化剂 → 多个资产）
-- ============================================================
-- 原 asset_id 字段保留（作为"主资产"快捷字段，取第一个/最重要的），
-- 完整关联走 N:N 表。这样既兼容旧查询，又支持多资产。

CREATE TABLE IF NOT EXISTS biz.catalyst_asset_link (
    catalyst_id    BIGINT NOT NULL REFERENCES biz.asset_catalyst(catalyst_id) ON DELETE CASCADE,
    asset_id       BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    link_source    VARCHAR(32) NOT NULL,  -- 关联来源：trading_pairs / cashtag / manual
    confidence     FLOAT NOT NULL DEFAULT 0.8,
    linked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (catalyst_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_catalyst_asset_link_asset
    ON biz.catalyst_asset_link (asset_id);

CREATE INDEX IF NOT EXISTS idx_catalyst_asset_link_catalyst
    ON biz.catalyst_asset_link (catalyst_id);

COMMENT ON TABLE biz.catalyst_asset_link IS '催化剂事件与资产的多对多关联（一篇新闻可能关联多个代币）';
COMMENT ON COLUMN biz.catalyst_asset_link.link_source IS '关联来源：trading_pairs（官方交易对字段）/ cashtag（正文cashtag提取）/ manual（人工标注）';

-- ============================================================
-- 第三步：source_codes 数组（多源合并时记录所有来源）
-- ============================================================

ALTER TABLE biz.asset_catalyst
    ADD COLUMN IF NOT EXISTS source_codes VARCHAR(32)[];

COMMENT ON COLUMN biz.asset_catalyst.source_codes
    IS '所有来源编码数组（跨源合并时记录，如 {binance_news, binance_square_news}）';

-- ============================================================
-- 第四步：为存量数据回填 content_hash + source_codes
-- ============================================================

-- 回填 source_codes（初始化为单元素数组）
UPDATE biz.asset_catalyst
SET source_codes = ARRAY[source_code]
WHERE source_codes IS NULL;

-- 回填 content_hash（对 title + body_text 前 200 字归一化后算 sha256）
UPDATE biz.asset_catalyst
SET content_hash = ENCODE(
    DIGEST(
        LOWER(
            REGEXP_REPLACE(
                COALESCE(title, '') || '|' || COALESCE(LEFT(body_text, 200), ''),
                '\s+', ' ', 'g'
            )
        ),
        'sha256'
    ),
    'hex'
)
WHERE content_hash IS NULL
  AND (title IS NOT NULL OR body_text IS NOT NULL);

-- ============================================================
-- 第五步：为存量数据回填多资产关联表
-- ============================================================

-- 从原 asset_id 字段回填（单资产 → N:N 表）
INSERT INTO biz.catalyst_asset_link (catalyst_id, asset_id, link_source, confidence)
SELECT catalyst_id, asset_id, 'legacy', 0.9
FROM biz.asset_catalyst
WHERE asset_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- ============================================================
-- 验证
-- ============================================================

SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE content_hash IS NOT NULL) AS with_hash,
    COUNT(*) FILTER (WHERE source_codes IS NOT NULL) AS with_source_codes
FROM biz.asset_catalyst;

SELECT COUNT(*) AS link_count FROM biz.catalyst_asset_link;
