-- 配额粗筛：从 doc_source_entry 中按规则筛选 NotebookLM 候选池
-- 规则：按 entry_type 配额 + 来源优先级 + deep_crawl 同域名限制
-- 输出：entry_id, entry_type, entry_url, source_code, is_deep_crawl, 域名

-- 最终步骤：合并所有 UNION ALL 结果，按 asset_id 分组后由 AI 排序

-- ============================================================
-- 1. whitepaper_page：全部纳入（最高优先级）
-- ============================================================
SELECT
    dse.entry_id AS source_entry_id,
    dse.asset_id,
    dse.entry_type,
    dse.entry_url,
    dse.source_code,
    (dse.discovered_from LIKE 'deep_crawl%') AS is_deep_crawl,
    LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
FROM biz.doc_source_entry dse
WHERE dse.asset_id = %s
  AND dse.entry_type = 'whitepaper_page'

UNION ALL

-- ============================================================
-- 2. docs：原始入口最多 5 + deep_crawl 补充（同域名 ≤ 2）
-- ============================================================
SELECT * FROM (
    -- 原始入口
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'docs'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    ORDER BY CASE WHEN dse.source_code IN ('cmc','cg') THEN 0 ELSE 1 END, dse.is_primary DESC
    LIMIT 5
) t1

UNION ALL

SELECT * FROM (
    -- deep_crawl 补充：同域名最多 2 条
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           TRUE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain,
           ROW_NUMBER() OVER (PARTITION BY LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) ORDER BY dse.entry_id) AS domain_rn
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'docs'
      AND dse.discovered_from LIKE 'deep_crawl%%'
) t2 WHERE t2.domain_rn <= 2
LIMIT 5

UNION ALL

-- ============================================================
-- 3. docs_portal：原始入口最多 3
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           (dse.discovered_from LIKE 'deep_crawl%') AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'docs_portal'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    ORDER BY CASE WHEN dse.source_code IN ('cmc','cg') THEN 0 ELSE 1 END, dse.is_primary DESC
    LIMIT 3
) t3

UNION ALL

-- ============================================================
-- 4. official_website：原始入口最多 3（is_primary 优先）
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'official_website'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    ORDER BY dse.is_primary DESC, CASE WHEN dse.source_code IN ('cmc','cg') THEN 0 ELSE 1 END
    LIMIT 3
) t4

UNION ALL

-- ============================================================
-- 5. github：只取原始入口，最多 2
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'github'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    ORDER BY CASE WHEN dse.source_code IN ('cmc','cg') THEN 0 ELSE 1 END
    LIMIT 2
) t5

UNION ALL

-- ============================================================
-- 6. medium：原始入口最多 3
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'medium'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    ORDER BY CASE WHEN dse.source_code IN ('cmc','cg') THEN 0 ELSE 1 END
    LIMIT 3
) t6

UNION ALL

-- ============================================================
-- 7. twitter：仅原始入口，最多 1
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'twitter'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    LIMIT 1
) t7

UNION ALL

-- ============================================================
-- 8. reddit：仅原始入口，最多 1
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'reddit'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    LIMIT 1
) t8

UNION ALL

-- ============================================================
-- 9. other：原始入口最多 3，deep_crawl 同域名 ≤ 2 补充
-- ============================================================
SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           FALSE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'other'
      AND dse.discovered_from NOT LIKE 'deep_crawl%%'
    ORDER BY CASE WHEN dse.source_code IN ('cmc','cg') THEN 0 ELSE 1 END, dse.is_primary DESC
    LIMIT 3
) t9a

UNION ALL

SELECT * FROM (
    SELECT dse.entry_id, dse.asset_id, dse.entry_type, dse.entry_url, dse.source_code,
           TRUE AS is_deep_crawl,
           LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) AS domain,
           ROW_NUMBER() OVER (PARTITION BY LOWER(SPLIT_PART(REPLACE(REPLACE(dse.entry_url, 'https://', ''), 'http://', ''), '/', 1)) ORDER BY dse.entry_id) AS domain_rn
    FROM biz.doc_source_entry dse
    WHERE dse.asset_id = %s AND dse.entry_type = 'other'
      AND dse.discovered_from LIKE 'deep_crawl%%'
) t9b WHERE t9b.domain_rn <= 2
LIMIT 5;