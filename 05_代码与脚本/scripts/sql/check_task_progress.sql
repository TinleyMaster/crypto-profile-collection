-- ============================================================
-- 各自动循环任务剩余量查询
-- ============================================================

-- 1. CG 拉取币种详情（自动循环）
--    还差多少个 coin info 没拉取
SELECT 'CG 拉取币种详情' AS task,
       (SELECT COUNT(*) FROM src_cg.coin_list) AS total_coins,
       (SELECT COUNT(*) FROM src_cg.coin_info) AS ingested,
       COUNT(*) AS remaining
FROM src_cg.coin_list l
LEFT JOIN src_cg.coin_info i ON i.coin_id = l.coin_id
WHERE i.coin_id IS NULL;

-- 2. CG 补充文档入口（自动循环）
--    还有多少 CG 资产没有 doc_source_entry（source_code='cg' 的）
WITH cg_assets AS (
    SELECT DISTINCT asm.asset_id
    FROM src_cg.coin_info i
    INNER JOIN core.asset_source_map asm ON asm.source_code = 'cg' AND asm.source_asset_key = i.coin_id
    WHERE i.homepage_url IS NOT NULL OR i.links IS NOT NULL
),
has_entry AS (
    SELECT DISTINCT asset_id FROM biz.doc_source_entry WHERE source_code = 'cg' AND entity_type = 'asset'
)
SELECT 'CG 补充文档入口' AS task,
       (SELECT COUNT(*) FROM cg_assets) AS total_candidates,
       (SELECT COUNT(*) FROM has_entry) AS with_entries,
       (SELECT COUNT(*) FROM cg_assets a WHERE a.asset_id NOT IN (SELECT asset_id FROM has_entry)) AS remaining;

-- 3. CMC 补充文档入口（自动循环）
--    还有多少 CMC 资产没有 doc_source_entry（source_code='cmc' 的）
WITH cmc_assets AS (
    SELECT DISTINCT asm.asset_id
    FROM src_cmc.cmc_asset_info i
    INNER JOIN core.asset_source_map asm ON asm.source_code = 'cmc' AND asm.source_asset_key = i.cmc_id::text
    WHERE i.urls IS NOT NULL
),
has_entry AS (
    SELECT DISTINCT asset_id FROM biz.doc_source_entry WHERE source_code = 'cmc' AND entity_type = 'asset'
)
SELECT 'CMC 补充文档入口' AS task,
       (SELECT COUNT(*) FROM cmc_assets) AS total_candidates,
       (SELECT COUNT(*) FROM has_entry) AS with_entries,
       (SELECT COUNT(*) FROM cmc_assets a WHERE a.asset_id NOT IN (SELECT asset_id FROM has_entry)) AS remaining;

-- 4. B2 深度文档发现（自动循环）
--    还有多少原始入口未爬取（deep_crawled_at IS NULL）
--    只统计文档类 entry_type（github/other/social 类不爬）
SELECT 'B2 深度文档发现' AS task,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE discovered_from NOT LIKE 'deep_crawl:%') AS total_original,
       COUNT(*) AS remaining_original,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE deep_crawled_at IS NOT NULL) AS crawled,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE discovered_from LIKE 'deep_crawl:%') AS deep_crawl_found
FROM biz.doc_source_entry
WHERE discovered_from NOT LIKE 'deep_crawl:%'
  AND deep_crawled_at IS NULL
  AND entry_type IN ('official_website', 'docs', 'docs_portal', 'medium', 'announcement',
                     'twitter', 'telegram', 'reddit', 'facebook');

-- 4b. B2 待爬取按类型明细
SELECT entry_type,
       COUNT(*) AS remaining
FROM biz.doc_source_entry
WHERE discovered_from NOT LIKE 'deep_crawl:%'
  AND deep_crawled_at IS NULL
GROUP BY entry_type
ORDER BY COUNT(*) DESC;

-- 5. B4 AI 噪声清理（自动循环）
--    还有多少 deep_crawl 链接未做 AI 筛选
SELECT 'B4 AI 噪声清理' AS task,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE discovered_from LIKE 'deep_crawl:%') AS total_deep_crawl,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE discovered_from LIKE 'deep_crawl:%' AND ai_noise_checked_at IS NOT NULL) AS ai_checked,
       COUNT(*) AS remaining_unchecked
FROM biz.doc_source_entry
WHERE discovered_from LIKE 'deep_crawl:%'
  AND ai_noise_checked_at IS NULL;

-- 6. 汇总概览
SELECT '--- 任务进度汇总 ---' AS summary,
       (SELECT COUNT(*) FROM src_cg.coin_list l LEFT JOIN src_cg.coin_info i ON i.coin_id = l.coin_id WHERE i.coin_id IS NULL) AS cg_coin_info_missing,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE discovered_from NOT LIKE 'deep_crawl:%' 
          AND deep_crawled_at IS NULL
          AND entry_type IN ('official_website', 'docs', 'docs_portal', 'medium', 'announcement',
                             'twitter', 'telegram', 'reddit', 'facebook')) AS b2_discovery_remaining,
       (SELECT COUNT(*) FROM biz.doc_source_entry 
        WHERE discovered_from LIKE 'deep_crawl:%' AND ai_noise_checked_at IS NULL) AS b2_ai_noise_remaining;
