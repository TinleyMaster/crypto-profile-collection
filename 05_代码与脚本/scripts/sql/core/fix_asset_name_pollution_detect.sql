-- =====================================================================
-- P0-1 资产名称污染检测脚本
-- 检测一个 asset_id 映射了多个 cmc_id 的情况（symbol 撞名导致的污染）
--
-- 用法：直接在 pgAdmin 或 psql 中执行，查看结果
-- =====================================================================

-- 1. 找出所有"一资产多 cmc_id"的污染案例
--    按 asset_id 分组，列出所有映射的 cmc_id 及其名称、rank、市值
WITH multi_mapped AS (
    SELECT asm.asset_id
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
)
SELECT
    a.asset_id,
    a.canonical_name AS current_name,
    a.canonical_symbol AS current_symbol,
    a.asset_type AS current_type,
    a.market_cap_rank AS current_rank,
    COUNT(*) AS cmc_id_count,
    -- 所有映射的 cmc_id 列表（JSON 数组）
    json_agg(
        json_build_object(
            'cmc_id', m.cmc_id,
            'name', m.name,
            'symbol', m.symbol,
            'rank', m.rank,
            'is_active', m.is_active
        )
        ORDER BY m.rank NULLS LAST
    ) AS mapped_cmc_ids
FROM core.asset a
JOIN multi_mapped mm ON mm.asset_id = a.asset_id
JOIN core.asset_source_map asm
    ON asm.asset_id = a.asset_id AND asm.source_code = 'cmc'
JOIN src_cmc.cmc_asset_map m ON m.cmc_id = asm.source_asset_key::bigint
GROUP BY a.asset_id, a.canonical_name, a.canonical_symbol, a.asset_type, a.market_cap_rank
ORDER BY a.market_cap_rank NULLS LAST, a.asset_id;


-- 2. 统计概览
SELECT
    COUNT(*) AS polluted_asset_count,
    SUM(cmc_id_count) AS total_cmc_mappings,
    SUM(cmc_id_count) - COUNT(*) AS extra_mappings_to_fix
FROM (
    SELECT asm.asset_id, COUNT(*) AS cmc_id_count
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
) t;


-- 3. 名称不一致检测（即使只有 1 个 cmc_id，但 canonical_name 与 CMC 官方名称不符）
SELECT
    a.asset_id,
    a.canonical_name AS core_name,
    m.name AS cmc_name,
    a.canonical_symbol AS symbol,
    a.market_cap_rank,
    a.asset_type
FROM core.asset a
JOIN core.asset_source_map asm
    ON asm.asset_id = a.asset_id AND asm.source_code = 'cmc'
JOIN src_cmc.cmc_asset_map m ON m.cmc_id = asm.source_asset_key::bigint
WHERE UPPER(a.canonical_name) != UPPER(m.name)
  AND a.market_cap_rank IS NOT NULL
ORDER BY a.market_cap_rank ASC
LIMIT 50;
