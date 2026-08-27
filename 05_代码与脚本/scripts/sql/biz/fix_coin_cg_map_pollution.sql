-- ============================================================
-- 批量清理 coin 资产的 CG source_map 同名币污染
--
-- 问题：
--   7,486 个 asset_type='coin' 的资产存在 cg_id != symbol 的映射
--   其中很多是同名币/仿盘/包装币污染，且大量资产没有 primary 映射
--
-- 修复策略（安全保守，避免误删真币映射）：
--   1. 对于有多个 CG 映射的 coin 资产：
--      a. 如果存在"非 symbol 匹配"的映射（如 BTC->bitcoin），
--         则删除所有 symbol 完全匹配的映射（同名仿盘），
--         并将非 symbol 匹配中 confidence 最高的设为 primary
--      b. 如果全是 symbol 匹配的（多个同名币），保留 confidence 最高的
--   2. 对于只有一个 CG 映射的 coin 资产，如果没有 primary，设为 primary
-- ============================================================

BEGIN;

-- ========== 统计：修复前 ==========
SELECT '修复前' AS phase,
       COUNT(*) AS total_cg_mappings,
       COUNT(DISTINCT a.asset_id) AS coin_assets_with_cg,
       SUM(CASE WHEN asm.is_primary THEN 1 ELSE 0 END) AS primary_count
FROM core.asset_source_map asm
JOIN core.asset a ON a.asset_id = asm.asset_id
WHERE a.asset_type = 'coin' AND asm.source_code = 'cg';

-- ========== 第一步：单映射 coin 设为 primary ==========
-- 只有一个 CG 映射且不是 primary 的，直接设为 primary
UPDATE core.asset_source_map asm
SET is_primary = TRUE,
    match_confidence = 100.00,
    match_status = 'confirmed',
    verified_by = 'bulk_native_fix_20260819',
    verified_at = NOW(),
    updated_at = NOW()
FROM core.asset a
WHERE a.asset_id = asm.asset_id
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND asm.is_primary = FALSE
  AND (
      SELECT COUNT(*) FROM core.asset_source_map asm2
      WHERE asm2.asset_id = a.asset_id AND asm2.source_code = 'cg'
  ) = 1;

-- ========== 第二步：多映射 coin 的清理 ==========
-- 策略：优先保留非 symbol 匹配的映射（项目全名如 bitcoin/ripple），
--       删除 symbol 完全匹配的（同名仿盘）

-- 先删除 symbol 完全匹配的映射（仅当该资产同时存在非 symbol 匹配的映射时）
DELETE FROM core.asset_source_map asm
USING core.asset a
WHERE a.asset_id = asm.asset_id
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND LOWER(a.canonical_symbol) = asm.source_asset_key
  AND EXISTS (
      SELECT 1 FROM core.asset_source_map asm2
      WHERE asm2.asset_id = a.asset_id
        AND asm2.source_code = 'cg'
        AND LOWER(a.canonical_symbol) != asm2.source_asset_key
  );

-- ========== 第三步：给多映射 coin 设 primary ==========
-- 对于仍有多个 CG 映射的 coin，取 confidence 最高的设为 primary
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT a.asset_id,
               (
                   SELECT asm2.source_asset_key
                   FROM core.asset_source_map asm2
                   WHERE asm2.asset_id = a.asset_id AND asm2.source_code = 'cg'
                   ORDER BY asm2.match_confidence DESC, asm2.source_asset_key
                   LIMIT 1
               ) AS best_key
        FROM core.asset a
        WHERE a.asset_type = 'coin'
          AND (
              SELECT COUNT(*) FROM core.asset_source_map asm2
              WHERE asm2.asset_id = a.asset_id AND asm2.source_code = 'cg'
          ) > 1
          AND NOT EXISTS (
              SELECT 1 FROM core.asset_source_map asm2
              WHERE asm2.asset_id = a.asset_id AND asm2.source_code = 'cg'
                AND asm2.is_primary = TRUE
          )
    LOOP
        UPDATE core.asset_source_map
        SET is_primary = TRUE,
            match_confidence = 100.00,
            match_status = 'confirmed',
            verified_by = 'bulk_native_fix_20260819',
            verified_at = NOW(),
            updated_at = NOW()
        WHERE asset_id = r.asset_id
          AND source_code = 'cg'
          AND source_asset_key = r.best_key;
    END LOOP;
END $$;

-- ========== 第四步：删除剩余的非 primary 映射 ==========
-- 经过上面处理后，每个 coin 资产应该只有一个 primary CG 映射 + 0 或多个非 primary
-- 删除所有非 primary 的（剩余的同名币/包装币污染）
DELETE FROM core.asset_source_map asm
USING core.asset a
WHERE a.asset_id = asm.asset_id
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND asm.is_primary = FALSE
  AND EXISTS (
      SELECT 1 FROM core.asset_source_map asm2
      WHERE asm2.asset_id = a.asset_id
        AND asm2.source_code = 'cg'
        AND asm2.is_primary = TRUE
  );

-- ========== 统计：修复后 ==========
SELECT '修复后' AS phase,
       COUNT(*) AS total_cg_mappings,
       COUNT(DISTINCT a.asset_id) AS coin_assets_with_cg,
       SUM(CASE WHEN asm.is_primary THEN 1 ELSE 0 END) AS primary_count
FROM core.asset_source_map asm
JOIN core.asset a ON a.asset_id = asm.asset_id
WHERE a.asset_type = 'coin' AND asm.source_code = 'cg';

COMMIT;
