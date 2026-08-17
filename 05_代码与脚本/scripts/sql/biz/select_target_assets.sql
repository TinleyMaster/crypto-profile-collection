-- 目标资产清单：新币（date_launched >= 2025-01-01）∪ 已打主赛道的资产。
--
-- 用于节流批量补齐单个代币的投研资料（配合 scripts/bin/collect_assets_batch.py）。
-- 赛道信号来自 core.asset.primary_sector（由 refresh_asset_sectors.py 从 CMC tags
-- 归一化写入，方案一落地），替代早期 src_cmc.cmc_asset_info.tags 的临时 7 类映射。
--
-- 输出列：asset_id, canonical_symbol, canonical_name, date_launched,
--         is_new, sectors, entry_count, sector_priority
-- 排序：新币优先 → 赛道采集优先级降序（L2/AI/DeFi 优先）→ 已采文档数升序。
-- 赛道采集优先级与 sector.py 的 SECTOR_COLLECT_PRIORITY 保持一致。

WITH cmc AS (
    -- 一个资产可能映射多个 cmc_id，取 date_launched 最新的那条作为代表
    SELECT DISTINCT ON (m.asset_id)
           m.asset_id,
           i.date_launched
    FROM core.asset_source_map m
    JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.source_asset_key::bigint
    WHERE m.source_code = 'cmc'
    ORDER BY m.asset_id, i.date_launched DESC NULLS LAST
),
entry_counts AS (
    SELECT asset_id, count(*) AS entry_count
    FROM biz.doc_source_entry
    GROUP BY asset_id
)
SELECT
    a.asset_id,
    a.canonical_symbol,
    a.canonical_name,
    c.date_launched,
    (c.date_launched >= '2025-01-01') AS is_new,
    CASE WHEN a.primary_sector IS NOT NULL AND a.primary_sector != 'other'
         THEN ARRAY[a.primary_sector]
         ELSE '{}'::text[] END AS sectors,
    COALESCE(ec.entry_count, 0) AS entry_count,
    CASE a.primary_sector
        WHEN 'l2'          THEN 95
        WHEN 'ai'          THEN 90
        WHEN 'defi'        THEN 85
        WHEN 'rwa'         THEN 80
        WHEN 'gamefi'      THEN 75
        WHEN 'l1'          THEN 70
        WHEN 'depin'       THEN 65
        WHEN 'cex_token'   THEN 60
        WHEN 'derivatives' THEN 55
        WHEN 'infra'       THEN 50
        WHEN 'meme'        THEN 45
        ELSE 30
    END AS sector_priority
FROM core.asset a
LEFT JOIN cmc c ON c.asset_id = a.asset_id
LEFT JOIN entry_counts ec ON ec.asset_id = a.asset_id
WHERE (c.date_launched >= '2025-01-01'
       OR (a.primary_sector IS NOT NULL AND a.primary_sector != 'other'))
  AND a.canonical_name NOT LIKE '%Derivatives%'
  AND a.canonical_name NOT LIKE '%Bridged%'
ORDER BY (c.date_launched >= '2025-01-01') DESC,
         sector_priority DESC,
         c.date_launched DESC NULLS LAST,
         entry_count ASC,
         a.canonical_symbol ASC;
