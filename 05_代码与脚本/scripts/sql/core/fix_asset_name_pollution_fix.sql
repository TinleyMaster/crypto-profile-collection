-- =====================================================================
-- P0-1 资产名称污染修复脚本（批量版）
-- 修复"一资产多 cmc_id"的 symbol 撞名污染问题
--
-- 修复策略：
--   1. 对每个多映射的 asset_id，按 cmc_rank 确定"正主"（rank 最小的保留在原 asset）
--   2. 先修复原 asset 的 canonical_name（用正主 CMC 名称覆盖被污染的名称）
--   3. 其余 cmc_id 从原 asset 批量剥离，创建独立的新 core.asset 记录
--   4. 批量更新 asset_source_map 指向新 asset_id
--
-- 注意：合约地址暂不迁移，后续 CMC 流水线自动补全
--
-- 安全保证：
--   - 事务包裹，可回滚
--   - 先备份污染映射到备份表，再执行修复
--   - 幂等：重复执行不会重复创建（通过备份表 + match_method 去重）
--
-- 用法：
--   BEGIN;
--   -- 执行本脚本全部内容
--   -- 检查影响行数无误后 COMMIT; 否则 ROLLBACK;
-- 注意：cmc_asset_map.rank_num 是 CMC 排名字段
-- =====================================================================

-- ============================================================
-- 步骤 0：创建备份表（幂等：已存在则跳过）
-- ============================================================
CREATE TABLE IF NOT EXISTS core.asset_name_pollution_backup (
    backup_id SERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL,           -- 原污染 asset_id
    cmc_id BIGINT NOT NULL,             -- 被剥离的 cmc_id
    new_asset_id BIGINT,                -- 修复后分配的新 asset_id
    cmc_name TEXT,                      -- CMC 官方名称
    cmc_symbol TEXT,                    -- CMC 官方 symbol
    cmc_rank INT,                       -- CMC 排名
    fixed_at TIMESTAMPTZ,               -- 修复时间
    UNIQUE (asset_id, cmc_id)
);

-- ============================================================
-- 步骤 1：识别所有"一资产多 cmc_id"的污染案例
--         按 cmc_rank 排序，rank 最小的为正主（保留在原 asset）
--         其余为需要剥离的映射
-- ============================================================
WITH multi_mapped AS (
    SELECT asm.asset_id
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
),
ranked_mappings AS (
    SELECT
        asm.asset_id,
        m.cmc_id,
        m.name AS cmc_name,
        m.symbol AS cmc_symbol,
        m.rank_num AS cmc_rank,
        ROW_NUMBER() OVER (
            PARTITION BY asm.asset_id
            ORDER BY
                m.rank_num NULLS LAST,    -- rank 越小越优先（正主）
                m.cmc_id ASC               -- rank 相同时按 cmc_id 升序
        ) AS rn
    FROM core.asset_source_map asm
    JOIN multi_mapped mm ON mm.asset_id = asm.asset_id
    JOIN src_cmc.cmc_asset_map m ON m.cmc_id = asm.source_asset_key::bigint
    WHERE asm.source_code = 'cmc'
)
-- 将需要剥离的映射（rn > 1）插入备份表（幂等：已存在则跳过）
INSERT INTO core.asset_name_pollution_backup (asset_id, cmc_id, cmc_name, cmc_symbol, cmc_rank)
SELECT asset_id, cmc_id, cmc_name, cmc_symbol, cmc_rank
FROM ranked_mappings
WHERE rn > 1
ON CONFLICT (asset_id, cmc_id) DO NOTHING;

-- ============================================================
-- 步骤 2：修复原 asset 的 canonical_name（如果被 meme 币名称污染）
--         用正主 cmc_id 的 CMC 官方名称覆盖
--         注意：必须在剥离映射之前执行，否则剥离后 multi_mapped 为空
-- ============================================================
WITH multi_mapped AS (
    SELECT asm.asset_id
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
),
primary_mapping AS (
    SELECT DISTINCT ON (asm.asset_id)
        asm.asset_id,
        m.cmc_id,
        m.name AS cmc_name,
        m.symbol AS cmc_symbol
    FROM core.asset_source_map asm
    JOIN multi_mapped mm ON mm.asset_id = asm.asset_id
    JOIN src_cmc.cmc_asset_map m ON m.cmc_id = asm.source_asset_key::bigint
    WHERE asm.source_code = 'cmc'
    ORDER BY asm.asset_id, m.rank_num NULLS LAST, m.cmc_id ASC
)
UPDATE core.asset a
SET
    canonical_name = pm.cmc_name,
    canonical_symbol = pm.cmc_symbol,
    updated_at = NOW()
FROM primary_mapping pm
WHERE a.asset_id = pm.asset_id
  AND (
      UPPER(a.canonical_name) != UPPER(pm.cmc_name)
      OR UPPER(a.canonical_symbol) != UPPER(pm.cmc_symbol)
  );

-- ============================================================
-- 步骤 3：批量创建新 asset 并迁移映射
--         （仅处理尚未分配 new_asset_id 的记录）
-- ============================================================

-- 3a: 批量创建新 asset（从原 asset 继承类型和状态）
WITH pending AS (
    SELECT b.asset_id AS old_asset_id, b.cmc_id, b.cmc_name, b.cmc_symbol, b.cmc_rank
    FROM core.asset_name_pollution_backup b
    WHERE b.new_asset_id IS NULL
    ORDER BY b.cmc_rank NULLS LAST, b.asset_id, b.cmc_id
),
new_assets AS (
    INSERT INTO core.asset (
        canonical_symbol, canonical_name, asset_type, status,
        launch_date, description_short, created_at, updated_at
    )
    SELECT p.cmc_symbol, p.cmc_name, a.asset_type, a.status, NULL, NULL, NOW(), NOW()
    FROM pending p
    JOIN core.asset a ON a.asset_id = p.old_asset_id
    RETURNING asset_id, canonical_symbol, canonical_name
),
-- 用 row_number 关联回 pending（按相同排序）
pending_rn AS (
    SELECT p.*, ROW_NUMBER() OVER (ORDER BY p.cmc_rank NULLS LAST, p.old_asset_id, p.cmc_id) AS rn
    FROM pending p
),
new_assets_rn AS (
    SELECT na.*, ROW_NUMBER() OVER (ORDER BY na.asset_id) AS rn
    FROM new_assets na
),
mapping AS (
    SELECT pr.old_asset_id, pr.cmc_id, pr.cmc_name, pr.cmc_symbol, pr.cmc_rank, nar.asset_id AS new_asset_id
    FROM pending_rn pr
    JOIN new_assets_rn nar ON nar.rn = pr.rn
)
-- 3b: 更新备份表，记录 new_asset_id
UPDATE core.asset_name_pollution_backup b
SET new_asset_id = m.new_asset_id, fixed_at = NOW()
FROM mapping m
WHERE b.asset_id = m.old_asset_id AND b.cmc_id = m.cmc_id AND b.new_asset_id IS NULL;

-- 3c: 批量更新 asset_source_map（将 cmc_id 映射从旧 asset 改到新 asset）
UPDATE core.asset_source_map asm
SET asset_id = b.new_asset_id,
    match_method = 'pollution_split',
    match_confidence = 1.0,
    is_primary = true,
    updated_at = NOW()
FROM core.asset_name_pollution_backup b
WHERE asm.source_code = 'cmc'
  AND asm.source_asset_key = b.cmc_id::text
  AND asm.asset_id = b.asset_id
  AND b.new_asset_id IS NOT NULL
  AND asm.match_method != 'pollution_split';

-- 注：合约地址暂不迁移，后续 CMC 流水线自动补全

-- ============================================================
-- 步骤 4：验证修复结果
-- ============================================================

-- 4.1 检查是否还有"一资产多 cmc_id"的情况（应为 0）
SELECT COUNT(*) AS remaining_multi_mapped
FROM (
    SELECT asm.asset_id
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
) t;

-- 4.2 统计修复概览
SELECT
    COUNT(*) AS total_fixed_mappings,
    COUNT(DISTINCT asset_id) AS affected_assets,
    COUNT(DISTINCT new_asset_id) AS new_assets_created
FROM core.asset_name_pollution_backup
WHERE new_asset_id IS NOT NULL;

-- 4.3 列出所有新创建的 asset 及其映射
SELECT
    b.new_asset_id,
    a.canonical_symbol,
    a.canonical_name,
    b.cmc_id,
    b.cmc_rank,
    b.asset_id AS old_asset_id
FROM core.asset_name_pollution_backup b
JOIN core.asset a ON a.asset_id = b.new_asset_id
WHERE b.new_asset_id IS NOT NULL
ORDER BY b.cmc_rank NULLS LAST, b.new_asset_id;

-- ============================================================
-- 回滚方法（如需回滚，执行以下语句）：
--
-- BEGIN;
-- -- 1. 将 asset_source_map 映射恢复到原 asset
-- UPDATE core.asset_source_map asm
-- SET asset_id = b.asset_id,
--     match_method = 'symbol_match',
--     updated_at = NOW()
-- FROM core.asset_name_pollution_backup b
-- WHERE asm.source_code = 'cmc'
--   AND asm.source_asset_key = b.cmc_id::text
--   AND asm.asset_id = b.new_asset_id;
--
-- -- 2. 删除新创建的 asset
-- DELETE FROM core.asset
-- WHERE asset_id IN (
--     SELECT new_asset_id FROM core.asset_name_pollution_backup
--     WHERE new_asset_id IS NOT NULL
-- );
--
-- -- 3. 清空备份表（或保留做记录）
-- -- TRUNCATE core.asset_name_pollution_backup;
-- COMMIT;
-- ============================================================
