-- fix_035_rollup_wrong_eth_chain_data.sql
-- 回滚错误导入的数据：etherscan_label 来源的 eth 链数据实际是 Optimism 链的
-- （域名是 optimistic.etherscan.io，之前搞错了链）

-- 1. 从交易所地址表删除
DELETE FROM biz.onchain_exchange_wallet
WHERE chain = 'eth'
  AND source = 'etherscan_label';

-- 2. 从通用标签表删除
DELETE FROM biz.onchain_address_label
WHERE chain = 'eth'
  AND source = 'etherscan_label';

-- 3. 验证：确认 eth 链没有 etherscan_label 来源了
SELECT chain, source, COUNT(*) as cnt
FROM biz.onchain_exchange_wallet
WHERE source = 'etherscan_label'
GROUP BY chain, source;
