-- fix_034_upgrade_explorer_label_confidence.sql
-- 将区块浏览器官方来源的地址标签置信度从 medium 升级为 high
-- 官方标签（Etherscan/BSCScan/Arbiscan 等）权威度高，应标记为 high

-- 1. 通用标签表
UPDATE biz.onchain_address_label
SET confidence = 'high',
    updated_at = CURRENT_TIMESTAMP
WHERE confidence = 'medium'
  AND source IN (
    'auto_bscscan_csv',     -- BSCScan（默认源）
    'bscscan_label',        -- BSCScan
    'etherscan_label',      -- Etherscan
    'basescan_label',       -- BaseScan
    'arbiscan_label',       -- Arbiscan
    'polygonscan_label',    -- PolygonScan
    'snowtrace_label',      -- Snowtrace (Avalanche)
    'optimism_label',       -- Optimism Etherscan
    'ftmscan_label',        -- FtmScan (Fantom)
    'bttcscan_label',       -- BTTCScan
    'celoscan_label'        -- CeloScan
  );

-- 2. 交易所地址表（仅区块浏览器来源的）
UPDATE biz.onchain_exchange_wallet
SET confidence = 'high',
    updated_at = CURRENT_TIMESTAMP
WHERE confidence = 'medium'
  AND source IN (
    'auto_bscscan_csv',
    'bscscan_label',
    'etherscan_label',
    'basescan_label',
    'arbiscan_label',
    'polygonscan_label',
    'snowtrace_label',
    'optimism_label',
    'ftmscan_label',
    'bttcscan_label',
    'celoscan_label'
  );

-- 3. 验证：按来源统计 high 的数量
SELECT source, confidence, COUNT(*) as cnt
FROM biz.onchain_address_label
WHERE source LIKE '%scan%' OR source LIKE '%label' OR source = 'auto_bscscan_csv'
GROUP BY source, confidence
ORDER BY source, confidence;
