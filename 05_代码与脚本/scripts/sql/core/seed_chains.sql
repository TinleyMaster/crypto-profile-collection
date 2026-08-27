-- ============================================================
-- 初始化 core.chain 表：从 asset_contract 中提取所有出现过的链
--
-- 问题：core.chain 表 0 行，作为 schema 完整性缺陷
-- 修复：从已有合约数据中提取所有链名，批量插入 chain 表
-- ============================================================

INSERT INTO core.chain (chain_name, chain_slug, is_mainnet)
SELECT DISTINCT
    chain AS chain_name,
    chain AS chain_slug,
    TRUE AS is_mainnet
FROM core.asset_contract
WHERE chain IS NOT NULL AND chain != ''
  AND chain NOT IN (
      SELECT chain_name FROM core.chain  -- 防重复
  )
ORDER BY chain;

-- 统计结果
SELECT COUNT(*) AS total_chains FROM core.chain;
SELECT * FROM core.chain ORDER BY chain_id LIMIT 20;
