-- ============================================================
-- 官网 primary 裁决（全量重算）
-- 策略：来源可信度 + 同域名最短路径优先
-- 来源可信度：cmc(100) > binance(90) > dl(60) > cg(40) > dexscreener(30)
--
-- 用法：每次文档入口刷新后执行，确保每个资产只有一个主官网
-- ============================================================

-- 步骤1：先把所有 official_website 的 is_primary 重置为 false
UPDATE biz.doc_source_entry
SET is_primary = false,
    updated_at = NOW()
WHERE entry_type = 'official_website'
  AND entity_type = 'asset';

-- 步骤2：为每个资产选出唯一的 winner，设为 is_primary = true
-- 规则：
--   a. 按来源可信度排序（高的优先）
--   b. 同来源按路径深度排序（浅的优先）
--   c. 同路径深度按 URL 长度排序（短的优先）
WITH ranked AS (
    SELECT
        entry_id,
        asset_id,
        ROW_NUMBER() OVER (
            PARTITION BY asset_id
            ORDER BY
                CASE source_code
                    WHEN 'cmc' THEN 100
                    WHEN 'binance' THEN 90
                    WHEN 'dl' THEN 60
                    WHEN 'cg' THEN 40
                    WHEN 'dexscreener' THEN 30
                    ELSE 10
                END DESC,
                -- 路径深度：去掉协议+域名后，路径中 / 的数量+1
                LENGTH(
                    REGEXP_REPLACE(
                        REGEXP_REPLACE(entry_url, '^https?://[^/]+', ''),
                        '^/+|/+$', '', 'g'
                    )
                ) - LENGTH(
                    REPLACE(
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(entry_url, '^https?://[^/]+', ''),
                            '^/+|/+$', '', 'g'
                        ),
                        '/', ''
                    )
                ) + 1 ASC,
                LENGTH(entry_url) ASC
        ) as rn
    FROM biz.doc_source_entry
    WHERE entry_type = 'official_website'
      AND entity_type = 'asset'
),
winners AS (
    SELECT entry_id, asset_id
    FROM ranked
    WHERE rn = 1
)
UPDATE biz.doc_source_entry e
SET is_primary = true,
    updated_at = NOW()
FROM winners w
WHERE e.entry_id = w.entry_id;

-- ============================================================
-- 统计结果
-- ============================================================
SELECT
    COUNT(*) FILTER (WHERE has_primary) as assets_with_primary,
    COUNT(*) FILTER (WHERE NOT has_primary) as assets_no_website,
    COUNT(*) FILTER (WHERE multi_primary) as assets_multi_primary,
    ROUND(COUNT(*) FILTER (WHERE has_primary) * 100.0 / COUNT(*), 1) as pct_with_primary
FROM (
    SELECT
        a.asset_id,
        BOOL_OR(e.is_primary) as has_primary,
        COUNT(*) FILTER (WHERE e.is_primary) > 1 as multi_primary
    FROM core.asset a
    LEFT JOIN biz.doc_source_entry e
        ON e.asset_id = a.asset_id
        AND e.entry_type = 'official_website'
        AND e.entity_type = 'asset'
    GROUP BY a.asset_id
) t;
