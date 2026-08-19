-- ============================================================
-- 修复 Bitcoin 等原生链币种的合约 + source_map 污染
--
-- 问题：
--   1. BTC (asset_type='coin') 被绑定了 6 条非原生链合约（WBTC/包装币/桥接币）
--   2. BTC 的 asset_source_map 有 11 个 CG 映射（同名币/仿盘/包装币），
--      全部 is_primary=False，正确的 bitcoin 也未标记为 primary
--   3. core.chain 表为空，不依赖它，直接用 chain text 字段操作
--
-- 修复范围：所有 asset_type='coin' 的原生币
-- ============================================================

BEGIN;

-- ========== 第一步：清理合约污染 ==========
-- 对 asset_type='coin' 的资产，删除所有 is_native=False 的合约
-- （原生币不应该有非原生链的智能合约地址）

-- 先统计
SELECT COUNT(*) AS contracts_to_delete
FROM core.asset_contract ac
JOIN core.asset a ON a.asset_id = ac.asset_id
WHERE a.asset_type = 'coin'
  AND ac.is_native = FALSE;

-- 执行删除
DELETE FROM core.asset_contract
WHERE asset_id IN (
    SELECT asset_id FROM core.asset WHERE asset_type = 'coin'
)
  AND is_native = FALSE;

-- ========== 第二步：清理 asset_source_map 污染 ==========
-- 对 BTC (asset_id=2)，只保留 coin_id='bitcoin' 的 CG 映射，
-- 其余同名/仿盘/包装币全部删除，并将 bitcoin 设为 primary

-- 先统计 BTC 的 CG 映射
SELECT COUNT(*) AS btc_cg_mappings_before
FROM core.asset_source_map
WHERE asset_id = 2 AND source_code = 'cg';

-- 删除非 bitcoin 的 CG 映射（只保留真正的 Bitcoin）
DELETE FROM core.asset_source_map
WHERE asset_id = 2
  AND source_code = 'cg'
  AND source_asset_key != 'bitcoin';

-- 将正确的 bitcoin 映射设为 primary
UPDATE core.asset_source_map
SET is_primary = TRUE,
    match_confidence = 100.00,
    match_status = 'verified',
    verified_by = 'manual_fix_20260819',
    verified_at = NOW(),
    updated_at = NOW()
WHERE asset_id = 2
  AND source_code = 'cg'
  AND source_asset_key = 'bitcoin';

-- ========== 第三步：对其他主流原生币做同样的 source_map 清理 ==========
-- 找出所有 asset_type='coin' 且有多个 CG 映射的资产，按 symbol 匹配正确的 coin_id
-- （这里只处理已知的主流币，其他币保留现状避免误删）

-- ETH (asset_id 需查询)
DO $$
DECLARE
    eth_asset_id BIGINT;
BEGIN
    SELECT asset_id INTO eth_asset_id FROM core.asset WHERE canonical_symbol = 'ETH' AND asset_type = 'coin' LIMIT 1;
    IF eth_asset_id IS NOT NULL THEN
        -- 删除非 ethereum 的 CG 映射
        DELETE FROM core.asset_source_map
        WHERE asset_id = eth_asset_id
          AND source_code = 'cg'
          AND source_asset_key != 'ethereum';

        -- 设为 primary
        UPDATE core.asset_source_map
        SET is_primary = TRUE,
            match_confidence = 100.00,
            match_status = 'verified',
            verified_by = 'manual_fix_20260819',
            verified_at = NOW(),
            updated_at = NOW()
        WHERE asset_id = eth_asset_id
          AND source_code = 'cg'
          AND source_asset_key = 'ethereum';
    END IF;
END $$;

-- SOL
DO $$
DECLARE
    sol_asset_id BIGINT;
BEGIN
    SELECT asset_id INTO sol_asset_id FROM core.asset WHERE canonical_symbol = 'SOL' AND asset_type = 'coin' LIMIT 1;
    IF sol_asset_id IS NOT NULL THEN
        DELETE FROM core.asset_source_map
        WHERE asset_id = sol_asset_id
          AND source_code = 'cg'
          AND source_asset_key != 'solana';

        UPDATE core.asset_source_map
        SET is_primary = TRUE,
            match_confidence = 100.00,
            match_status = 'verified',
            verified_by = 'manual_fix_20260819',
            verified_at = NOW(),
            updated_at = NOW()
        WHERE asset_id = sol_asset_id
          AND source_code = 'cg'
          AND source_asset_key = 'solana';
    END IF;
END $$;

-- ========== 验证 ==========

-- BTC 合约情况
SELECT 'BTC contracts' AS check_type,
       ac.contract_id, ac.chain, ac.contract_address, ac.is_native, ac.is_primary, ac.source_code
FROM core.asset_contract ac
WHERE ac.asset_id = 2
ORDER BY ac.contract_id;

-- BTC source_map 情况
SELECT 'BTC source_map' AS check_type,
       asm.source_code, asm.source_asset_key, asm.is_primary, asm.match_confidence, asm.match_status
FROM core.asset_source_map asm
WHERE asm.asset_id = 2
ORDER BY asm.source_code, asm.is_primary DESC;

COMMIT;
