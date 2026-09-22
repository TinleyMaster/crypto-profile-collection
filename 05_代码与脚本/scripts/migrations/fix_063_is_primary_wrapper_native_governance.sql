-- fix_063: is_primary 错标包装/桥接变体治理（续，工单 W5 扩展，2026-09-22）
--
-- fix_062 处理了 9 个 binance-peg-* 无歧义资产。本迁移处理另外 3 个
-- 「primary 为包装/桥接变体、候选为本体币（名称一致且不含 bridged/wrapped）」的资产：
--   CRO(1588)   cronos-zkevm-bridged-cro-cronos-zkevm -> crypto-com-chain
--   SEI(3789)   layerzero-bridged-sei                  -> sei-network
--   CETH(14311) wrapped-ceth                           -> compound-ether
--
-- 已对 prod 执行（2026-09-22）。其余「primary 为包装变体但候选同为包装」的 4 个
-- （ABTC/GWBTC/LINK.E/MUSDC，资产本身即桥接币）**不处理**——翻转只会换一个包装，无收益且可能错。
--
-- 幂等：WHERE 子句保证重复执行为 0 行。

BEGIN;

CREATE TEMP TABLE _w5c_before ON COMMIT DROP AS
SELECT a.asset_id, a.canonical_symbol, asm.source_asset_key AS old_primary_cg_id
FROM core.asset a
JOIN core.asset_source_map asm
  ON asm.asset_id = a.asset_id AND asm.source_code = 'cg' AND asm.is_primary = TRUE
WHERE a.asset_id IN (1588, 3789, 14311);

-- 1) 取消错标 primary
UPDATE core.asset_source_map asm
SET is_primary = FALSE, updated_at = NOW()
FROM _w5c_before b
WHERE asm.asset_id = b.asset_id AND asm.source_code = 'cg'
  AND asm.source_asset_key = b.old_primary_cg_id AND asm.is_primary = TRUE;

-- 2) 设正确本体币为 primary
UPDATE core.asset_source_map
SET is_primary = TRUE, match_status = 'confirmed',
    verified_by = 'w5_is_primary_governance_20260922',
    verified_at = NOW(), updated_at = NOW()
WHERE source_code = 'cg'
  AND (asset_id, source_asset_key) IN (
      (1588, 'crypto-com-chain'),
      (3789, 'sei-network'),
      (14311, 'compound-ether')
  );

COMMIT;
