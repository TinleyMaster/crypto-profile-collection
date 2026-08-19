-- ============================================================
-- 修复 P3-⑨ 副作用：6 个主流币 primary cg_id 错配
--
-- 背景：
--   fix_coin_cg_map_pollution.sql 执行后，6 个主流币的 primary cg_id
--   被错误地设为包装币/桥接币/同名狗币（如 ADA -> ada-the-dog）。
--   根因：SQL 假设"非 symbol 匹配 = 真币"，但包装币 cg_id 也不等于 symbol。
--
-- 修复策略：
--   1. 先取消 6 个主流币当前的错误 primary 映射
--   2. 将正确的 cg_id 设为 primary（基于 CoinGecko 官方命名）
--   3. 删除剩余的非 primary 污染映射
--
-- 真币 cg_id 对照（CoinGecko 官方）：
--   ADA  -> cardano
--   XRP  -> ripple
--   DOGE -> dogecoin
--   BNB  -> binancecoin  (CG 上 BNB 的 id 是 binancecoin)
--   TRX  -> tron
--   DOT  -> polkadot
-- ============================================================

BEGIN;

-- ========== 修复前检查 ==========
SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
       asm.source_asset_key AS current_primary_cg_id,
       asm.match_confidence
FROM core.asset a
JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id
WHERE a.canonical_symbol IN ('ADA','XRP','DOGE','BNB','TRX','DOT')
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND asm.is_primary = TRUE
ORDER BY a.canonical_symbol;

-- ========== 第一步：取消当前错误的 primary ==========
UPDATE core.asset_source_map
SET is_primary = FALSE,
    updated_at = NOW()
WHERE asset_id IN (
    SELECT asset_id FROM core.asset
    WHERE canonical_symbol IN ('ADA','XRP','DOGE','BNB','TRX','DOT')
      AND asset_type = 'coin'
)
AND source_code = 'cg'
AND is_primary = TRUE;

-- ========== 第二步：将正确的 cg_id 设为 primary ==========
-- 使用 CTE 为每个资产匹配正确的 cg_id
WITH correct_mappings (symbol, correct_cg_id) AS (
    VALUES
        ('ADA',  'cardano'),
        ('XRP',  'ripple'),
        ('DOGE', 'dogecoin'),
        ('BNB',  'binancecoin'),
        ('TRX',  'tron'),
        ('DOT',  'polkadot')
)
UPDATE core.asset_source_map asm
SET is_primary = TRUE,
    match_confidence = 100.00,
    match_status = 'confirmed',
    verified_by = 'fix_coin_cg_primary_mismatch_20260819',
    verified_at = NOW(),
    updated_at = NOW()
FROM core.asset a,
     correct_mappings cm
WHERE a.asset_id = asm.asset_id
  AND a.canonical_symbol = cm.symbol
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND asm.source_asset_key = cm.correct_cg_id;

-- ========== 第三步：删除剩余的非 primary 污染映射 ==========
-- （包装币/桥接币/同名狗币等）
DELETE FROM core.asset_source_map asm
USING core.asset a
WHERE a.asset_id = asm.asset_id
  AND a.canonical_symbol IN ('ADA','XRP','DOGE','BNB','TRX','DOT')
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND asm.is_primary = FALSE;

-- ========== 修复后验证 ==========
SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
       asm.source_asset_key AS primary_cg_id,
       asm.match_confidence,
       asm.verified_by
FROM core.asset a
JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id
WHERE a.canonical_symbol IN ('ADA','XRP','DOGE','BNB','TRX','DOT')
  AND a.asset_type = 'coin'
  AND asm.source_code = 'cg'
  AND asm.is_primary = TRUE
ORDER BY a.canonical_symbol;

COMMIT;
