-- ============================================================
-- 交易所钱包地址种子库（主流 CEX 热钱包 / 冷钱包）
-- 来源：Etherscan / BSCScan 公开标签
-- 说明：
--   1. 仅包含公开可验证的高可信度地址
--   2. 后续可通过链上监控 / Arkham / Nansen 持续扩充
--   3. 用于标注 onchain_transfer_log 的发送方/接收方是否为交易所
-- ============================================================

-- 确保表存在
CREATE TABLE IF NOT EXISTS biz.onchain_exchange_wallet (
    wallet_id      SERIAL PRIMARY KEY,
    address        TEXT   NOT NULL,
    exchange_name  TEXT   NOT NULL,
    chain          TEXT   NOT NULL,
    label          TEXT   DEFAULT 'exchange',
    confidence     TEXT   DEFAULT 'high',
    source         TEXT   DEFAULT 'seed',
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_exchange_wallet UNIQUE (address, chain)
);

CREATE INDEX IF NOT EXISTS idx_exchange_wallet_address
    ON biz.onchain_exchange_wallet (address, chain);

-- ============================================================
-- Binance (ETH) - 高可信度
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    -- Binance 14 (冷钱包)
    ('0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Binance 10 (冷钱包)
    ('0xf977814e90da44bfa03b6295a0616a897441acec', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Binance 热钱包
    ('0x28C6c06298d514Db089934071355E5743bf21d60', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Binance 热钱包 2
    ('0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Binance 热钱包 3
    ('0xDFd5293D8e347dFe59E90eFd55b2956a1343963d', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Binance 热钱包 4
    ('0x5a52e96bacdabb82fd05763e25335261b270efcb', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Binance 热钱包 5
    ('0x47ac0Fb4F2D84898e4D9E7b4DaB3C24507a6D503', 'Binance', 'eth', 'exchange', 'high', 'etherscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- Binance (BSC) - 高可信度
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    -- Binance BSC 热钱包
    ('0x8894E0a0c962CB723c1976a4421c95949bE2D4E3', 'Binance', 'bsc', 'exchange', 'high', 'bscscan-label'),
    -- Binance BSC 热钱包 2
    ('0x0D0707963952f2fBA59dD06f2b425ace40b492Fe', 'Binance', 'bsc', 'exchange', 'high', 'bscscan-label'),
    -- Binance BSC 热钱包 3
    ('0x18b2a687610328590bc8f2e5fedde3b582a49cda', 'Binance', 'bsc', 'exchange', 'medium', 'bscscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- Coinbase (ETH) - 高可信度
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    -- Coinbase 冷钱包
    ('0x71660c4005BA85476C0FE5d080f20C20e7b61C94', 'Coinbase', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Coinbase 热钱包
    ('0x503828976D22510aA0d5d6b773756A3e02c1b97f', 'Coinbase', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- Coinbase 热钱包 2
    ('0xA090e606E30bD747d4E6245a1517EbE430F0057e', 'Coinbase', 'eth', 'exchange', 'high', 'etherscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- OKX (ETH) - 高可信度
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    -- OKX 热钱包
    ('0x6CC14824Ea2918f5De5C2f75A9Da968ad4BD6344', 'OKX', 'eth', 'exchange', 'high', 'etherscan-label'),
    -- OKX 热钱包 2
    ('0x9696f59E4d72E237d85aB7F66B9eB7d5bB7eB7d5', 'OKX', 'eth', 'exchange', 'high', 'etherscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- Huobi (ETH) - 高可信度
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    -- Huobi 冷钱包
    ('0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B', 'Huobi', 'eth', 'exchange', 'high', 'etherscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- Coinbase (BSC) - 高可信度（BSCScan 标签）
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    ('0x3c783c21a0383057D128bAe4318B9Cfc870298C4', 'Coinbase', 'bsc', 'exchange', 'high', 'bscscan-label'),
    ('0x599d6FA1CAE44E6B99E1a3B6C1e49a93be934892', 'Coinbase', 'bsc', 'exchange', 'high', 'bscscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- OKX (BSC) - 高可信度（BSCScan 标签）
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    ('0x6aA6a0F7B50d43F39b5B4C6521F3cA8888b070C0', 'OKX', 'bsc', 'exchange', 'high', 'bscscan-label'),
    ('0x242cF37340B23B87a56B5b7D78C06c2640564900', 'OKX', 'bsc', 'exchange', 'high', 'bscscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- KuCoin (BSC) - 高可信度（BSCScan 标签）
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    ('0xd6216fC19DB775Df9774a6E33526131dA7D19a2c', 'KuCoin', 'bsc', 'exchange', 'high', 'bscscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- Binance (TRON) - 高可信度（TRONScan 标签）
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    ('TFRyoLeBd4MUha4F4pCmjq3mPMPmWEWqxB', 'Binance', 'tron', 'exchange', 'high', 'tronscan-label'),
    ('THhKHh1bMJonU8V8VCdFb2dGvpi4eMn4Vc', 'Binance', 'tron', 'exchange', 'high', 'tronscan-label'),
    ('TJDENsfBJs4RFETt1X1W8wMDc8M5XnJhCe', 'Binance', 'tron', 'exchange', 'high', 'tronscan-label'),
    ('TMnaj37Bf3MK1DSbNRnKzkbvA9Uc1ZjR6p', 'Binance', 'tron', 'exchange', 'high', 'tronscan-label'),
    ('TVGDf8nMmQi8Txb7Z6uK6VwTi1TP5hD6pJ', 'Binance', 'tron', 'exchange', 'high', 'tronscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- OKX (TRON) - 高可信度（TRONScan 标签）
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    ('TFRoVfMCz7dBVzUJQf4bZ7VN4ab6XvZ7xD', 'OKX', 'tron', 'exchange', 'high', 'tronscan-label'),
    ('TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb', 'OKX', 'tron', 'exchange', 'high', 'tronscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- Huobi (TRON) - 高可信度（TRONScan 标签）
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet (address, exchange_name, chain, label, confidence, source) VALUES
    ('THDmzHMzECN3aMUy5xqJaQxUjFENv3uDcP', 'Huobi', 'tron', 'exchange', 'high', 'tronscan-label'),
    ('TNaRA1RFrzm9JPh3E6VC4C3VZPt6sW7VgZ', 'Huobi', 'tron', 'exchange', 'high', 'tronscan-label')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- 统计信息
-- ============================================================
-- 执行后可运行以下查询确认：
-- SELECT exchange_name, chain, COUNT(*)
-- FROM biz.onchain_exchange_wallet
-- GROUP BY exchange_name, chain
-- ORDER BY exchange_name, chain;
