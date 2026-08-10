-- ============================================================
-- 综合数据修复迁移
-- FIX-003: 白皮书 CHECK 约束补全 + 回填
-- FIX-005: GitHub 漏标修复
-- FIX-008: 空 symbol 资产修复 + lifecycle 列
-- 
-- 执行前建议先 pg_dump 备份
-- 所有操作零 API 成本，纯从 raw.api_response 解析
-- ============================================================

BEGIN;

-- ============================================================
-- FIX-003: 补全 entry_type CHECK 约束
-- ============================================================
ALTER TABLE biz.doc_source_entry DROP CONSTRAINT IF EXISTS chk_doc_source_entry_type;
ALTER TABLE biz.doc_source_entry ADD CONSTRAINT chk_doc_source_entry_type
CHECK (entry_type = ANY (ARRAY[
    'official_website','docs','docs_portal','github','medium','announcement',
    'twitter','telegram','reddit','facebook','other',
    'whitepaper','audit','forum','explorer','blog'
]::varchar[]));

-- FIX-003 步骤2: 用 CoinGecko ground truth 重标已在库的白皮书
WITH cg AS (
    SELECT DISTINCT ON (payload->>'id')
        payload->>'id' AS cg_id,
        NULLIF(payload->'links'->>'whitepaper', '') AS wp
    FROM raw.api_response
    WHERE endpoint_code = 'coin_info'
    ORDER BY payload->>'id', fetched_at DESC
), w AS (
    SELECT m.asset_id,
        regexp_replace(regexp_replace(lower(cg.wp), '^https?://(www\.)?', ''), '/+$', '') AS wpn
    FROM cg
    JOIN core.asset_source_map m
        ON m.source_code = 'cg' AND m.source_asset_key = cg.cg_id
    WHERE cg.wp IS NOT NULL
)
UPDATE biz.doc_source_entry e
SET entry_type = 'whitepaper', updated_at = now()
FROM w
WHERE e.asset_id = w.asset_id
  AND regexp_replace(regexp_replace(lower(e.entry_url), '^https?://(www\.)?', ''), '/+$', '') = w.wpn;

-- FIX-003 步骤3: 插入库外白皮书
INSERT INTO biz.doc_source_entry (entity_type, asset_id, source_code, entry_type, entry_url, discovered_from)
SELECT 'asset', w.asset_id, 'cg', 'whitepaper', w.wp, 'cg_links_whitepaper'
FROM (
    SELECT DISTINCT ON (payload->>'id')
        payload->>'id' AS cg_id,
        NULLIF(payload->'links'->>'whitepaper', '') AS wp
    FROM raw.api_response
    WHERE endpoint_code = 'coin_info'
    ORDER BY payload->>'id', fetched_at DESC
) cg
JOIN core.asset_source_map m
    ON m.source_code = 'cg' AND m.source_asset_key = cg.cg_id
CROSS JOIN LATERAL (SELECT cg.wp) AS w(wp)
JOIN core.asset a ON a.asset_id = m.asset_id
WHERE cg.wp IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM biz.doc_source_entry e
      WHERE e.asset_id = m.asset_id
        AND regexp_replace(regexp_replace(lower(e.entry_url), '^https?://(www\.)?', ''), '/+$', '') =
            regexp_replace(regexp_replace(lower(cg.wp), '^https?://(www\.)?', ''), '/+$', '')
  )
ON CONFLICT DO NOTHING;

-- ============================================================
-- FIX-005: GitHub 漏标修复
-- ============================================================
WITH cg AS (
    SELECT DISTINCT ON (payload->>'id')
        payload->>'id' AS cg_id,
        payload->'links'->'repos_url'->'github' AS gh_arr
    FROM raw.api_response
    WHERE endpoint_code = 'coin_info'
    ORDER BY payload->>'id', fetched_at DESC
), gh AS (
    SELECT m.asset_id,
        regexp_replace(regexp_replace(lower(g.url), '^https?://(www\.)?', ''), '/+$', '') AS ghn
    FROM cg
    JOIN core.asset_source_map m
        ON m.source_code = 'cg' AND m.source_asset_key = cg.cg_id
    CROSS JOIN LATERAL jsonb_array_elements_text(cg.gh_arr) AS g(url)
    WHERE cg.gh_arr IS NOT NULL
)
UPDATE biz.doc_source_entry e
SET entry_type = 'github', updated_at = now()
FROM gh
WHERE e.asset_id = gh.asset_id
  AND regexp_replace(regexp_replace(lower(e.entry_url), '^https?://(www\.)?', ''), '/+$', '') = gh.ghn
  AND e.entry_type <> 'github';

-- ============================================================
-- FIX-008: 空 symbol 资产修复 + lifecycle 列
-- ============================================================
-- 8a. 添加 lifecycle 列
ALTER TABLE core.asset ADD COLUMN IF NOT EXISTS lifecycle VARCHAR(20) DEFAULT 'active';
COMMENT ON COLUMN core.asset.lifecycle IS 'active / delisted / migrated';

-- 8b. 回填空 symbol（用 coin_basic 和 asset_source_map）
UPDATE core.asset a
SET canonical_symbol = sub.symbol
FROM (
    SELECT DISTINCT ON (a.asset_id)
        a.asset_id,
        COALESCE(cb.symbol, m.source_asset_key) AS symbol
    FROM core.asset a
    LEFT JOIN core.asset_source_map m ON a.asset_id = m.asset_id
    LEFT JOIN raw.coin_basic cb ON LOWER(cb.symbol) = LOWER(m.source_asset_key)
    WHERE a.canonical_symbol IS NULL OR a.canonical_symbol = ''
) sub
WHERE a.asset_id = sub.asset_id
  AND (a.canonical_symbol IS NULL OR a.canonical_symbol = '');

COMMIT;

-- ============================================================
-- 验证
-- ============================================================
SELECT 'FIX-003' AS fix, COUNT(*) AS count FROM biz.doc_source_entry WHERE entry_type = 'whitepaper'
UNION ALL
SELECT 'FIX-005', COUNT(*) FROM biz.doc_source_entry WHERE entry_type = 'github'
UNION ALL
SELECT 'FIX-008a', COUNT(*) FROM core.asset WHERE canonical_symbol IS NULL OR canonical_symbol = ''
UNION ALL
SELECT 'FIX-008b', COUNT(*) FROM core.asset WHERE lifecycle = 'active';