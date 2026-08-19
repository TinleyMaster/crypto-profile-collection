-- ============================================================
-- 修复 Bitcoin 等原生链币种的合约污染问题
--
-- 问题：BTC/ETH/SOL 等原生链币种（asset_type='coin'）被错误绑定了
--       非原生链的 EVM 合约地址（如 WBTC 包装代币的合约），
--       导致链上查询、持仓快照等功能使用错误地址。
--
-- 修复：
--   1. 对 asset_type='coin' 的资产，删除非原生链的合约
--   2. 确保原生链合约标记 is_native=true
--   3. 对 BTC 这类没有智能合约的纯原生币，保留一条 is_native=true 的记录
--      （contract_address 可以为空或填 'native'）
-- ============================================================

BEGIN;

-- 第一步：查看受影响的原生币资产（用于审计）
CREATE TEMP TABLE _affected_coins AS
SELECT a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type,
       COUNT(ac.contract_id) AS total_contracts,
       COUNT(ac.contract_id) FILTER (WHERE ac.is_native = TRUE) AS native_contracts,
       COUNT(ac.contract_id) FILTER (WHERE ac.is_native = FALSE) AS non_native_contracts
FROM core.asset a
LEFT JOIN core.asset_contract ac ON ac.asset_id = a.asset_id
WHERE a.asset_type = 'coin'
GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name, a.asset_type
HAVING COUNT(ac.contract_id) FILTER (WHERE ac.is_native = FALSE) > 0
ORDER BY non_native_contracts DESC;

RAISE NOTICE '受影响的原生币数量: %', (SELECT COUNT(*) FROM _affected_coins);

-- 第二步：删除原生币的非原生链合约（如 BTC 的 EVM 合约）
-- 保留 is_native=true 的记录
DELETE FROM core.asset_contract
WHERE asset_id IN (SELECT asset_id FROM _affected_coins)
  AND is_native = FALSE;

-- 第三步：确保每个原生币至少有一条 is_native=true 的合约记录
-- （如果没有，插入一条标记为原生的记录，contract_address='native'）
INSERT INTO core.asset_contract (asset_id, chain_id, contract_address, is_native, is_primary, source_preference)
SELECT
    a.asset_id,
    (SELECT chain_id FROM core.chain WHERE chain_code = LOWER(a.canonical_symbol) LIMIT 1) AS chain_id,
    'native' AS contract_address,
    TRUE AS is_native,
    TRUE AS is_primary,
    'manual' AS source_preference
FROM core.asset a
WHERE a.asset_type = 'coin'
  AND NOT EXISTS (
      SELECT 1 FROM core.asset_contract ac
      WHERE ac.asset_id = a.asset_id AND ac.is_native = TRUE
  );

-- 第四步：将 BTC 的 asset_type 确认为 'coin'（如果之前是 token）
UPDATE core.asset
SET asset_type = 'coin',
    updated_at = NOW()
WHERE canonical_symbol = 'BTC'
  AND asset_type != 'coin';

-- 第五步：清理 nos 链（未知链）的合约，这些通常是污染数据
-- 先统计
DO $$
DECLARE
    nos_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO nos_count
    FROM core.asset_contract ac
    JOIN core.chain c ON c.chain_id = ac.chain_id
    WHERE c.chain_code = 'nos';

    RAISE NOTICE 'nos 链合约数量: %', nos_count;
END $$;

-- 删除 nos 链的合约（这些是未识别链，通常是数据污染）
DELETE FROM core.asset_contract
WHERE chain_id IN (SELECT chain_id FROM core.chain WHERE chain_code = 'nos');

COMMIT;

-- 验证：查询 BTC 的合约情况
SELECT a.asset_id, a.canonical_symbol, a.asset_type,
       ac.contract_id, c.chain_code, ac.contract_address, ac.is_native, ac.is_primary
FROM core.asset a
LEFT JOIN core.asset_contract ac ON ac.asset_id = a.asset_id
LEFT JOIN core.chain c ON c.chain_id = ac.chain_id
WHERE a.canonical_symbol = 'BTC'
ORDER BY ac.is_native DESC, ac.contract_id;
