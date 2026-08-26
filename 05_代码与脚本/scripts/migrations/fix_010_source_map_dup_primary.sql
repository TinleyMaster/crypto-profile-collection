-- FIX-010: source_map 重复 is_primary 修复 + 防回归约束
-- 审计日期：2026-08-26
-- 问题：同一 asset_id 存在多条 is_primary=true 的记录（cg + cmc 双写）
-- 修复：1) 清洗现有 618 组重复，保留 cg 为 primary，cmc 降级
--       2) 加 partial unique index 强制约束，防止回归

-- ============================================================
-- 第一步：清洗现有脏数据（保留 cg 为 primary，cmc 降级）
-- ============================================================

-- 先备份受影响的行（用于回滚）
CREATE TABLE IF NOT EXISTS public._bak_source_map_dup_primary_20260826 AS
SELECT *
FROM core.asset_source_map
WHERE asset_id IN (
    SELECT asset_id
    FROM core.asset_source_map
    WHERE is_primary = TRUE
    GROUP BY asset_id
    HAVING COUNT(*) > 1
)
  AND is_primary = TRUE
ORDER BY asset_id, source_code;

-- 将 cmc 的重复 primary 降级为 false
-- 优先级：cg > cmc > dl（按 8/25 既定口径）
-- 注意：不改 match_method（varchar(32) 长度受限，且原值已足够溯源）
UPDATE core.asset_source_map asm
SET is_primary = FALSE,
    updated_at = NOW()
FROM (
    SELECT asset_id, source_code, source_asset_key
    FROM (
        SELECT asset_id, source_code, source_asset_key,
               ROW_NUMBER() OVER (
                   PARTITION BY asset_id
                   ORDER BY CASE source_code
                       WHEN 'cg' THEN 1
                       WHEN 'cmc' THEN 2
                       WHEN 'dl' THEN 3
                       ELSE 99
                   END
               ) AS rn
        FROM core.asset_source_map
        WHERE is_primary = TRUE
    ) sub
    WHERE rn > 1
) dup
WHERE asm.asset_id = dup.asset_id
  AND asm.source_code = dup.source_code
  AND asm.source_asset_key = dup.source_asset_key;

-- ============================================================
-- 第二步：加 partial unique index 强制约束（防回归）
-- ============================================================

-- 同一 asset_id 只能有一条 is_primary=true 的记录
CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_source_map_primary_true
    ON core.asset_source_map (asset_id)
    WHERE is_primary = TRUE;

-- ============================================================
-- 验证
-- ============================================================

-- 验证：重复 primary 应为 0
SELECT COUNT(*) AS remaining_dup_primary
FROM (
    SELECT asset_id
    FROM core.asset_source_map
    WHERE is_primary = TRUE
    GROUP BY asset_id
    HAVING COUNT(*) > 1
) sub;

-- 验证：primary 总数应等于资产数（有 primary 映射的资产）
SELECT COUNT(*) AS primary_count
FROM core.asset_source_map
WHERE is_primary = TRUE;
