-- ============================================================
-- 修复 primary 官网污染（批量 SQL 版本）
-- 策略：来源可信度 + 同域名最短路径优先
-- 来源可信度：cmc(100) > binance(90) > dl(60) > cg(40) > dexscreener(30)
-- ============================================================

-- 步骤1：为每个资产选出唯一的 winner（应该保留 primary 的那条）
-- 规则：
--   a. 按来源可信度排序（高的优先）
--   b. 同来源按路径深度排序（浅的优先）
--   c. 同路径深度按 URL 长度排序（短的优先）
-- 步骤2：把其他所有 primary 降级为非 primary

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
                -- 路径深度：计算 / 的数量（去掉首尾的）
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
      AND is_primary = true
      AND entity_type = 'asset'
),
winners AS (
    SELECT entry_id, asset_id
    FROM ranked
    WHERE rn = 1
),
to_demote AS (
    SELECT r.entry_id
    FROM ranked r
    WHERE r.rn > 1
)
UPDATE biz.doc_source_entry e
SET is_primary = false,
    updated_at = NOW()
FROM to_demote d
WHERE e.entry_id = d.entry_id;

-- ============================================================
-- 验证：修复后还有多少多 primary 官网资产
-- ============================================================
SELECT COUNT(*) as remaining_multi_primary
FROM (
    SELECT asset_id
    FROM biz.doc_source_entry
    WHERE entry_type = 'official_website'
      AND is_primary = true
      AND entity_type = 'asset'
    GROUP BY asset_id
    HAVING COUNT(*) > 1
) t;

-- ============================================================
-- 验证：Bitcoin 的官网
-- ============================================================
SELECT source_code, entry_url, is_primary
FROM biz.doc_source_entry
WHERE asset_id = 2
  AND entry_type = 'official_website'
  AND entity_type = 'asset'
ORDER BY is_primary DESC, source_code;
