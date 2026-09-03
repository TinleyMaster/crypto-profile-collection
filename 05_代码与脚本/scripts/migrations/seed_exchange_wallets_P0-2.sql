-- P0-2 净流轴补 BSC/TRON 交易所钱包种子
-- 风险：写 prod 库 biz.onchain_exchange_wallet，需女王单独口头授权后执行
-- 执行方式：psql $DATABASE_URL -f 05_代码与脚本/scripts/migrations/seed_exchange_wallets_P0-2.sql
--
-- 设计原则：
--   1. 幂等：ON CONFLICT (address, chain) DO NOTHING
--   2. 高可信度：来自 Etherscan/BSCScan/TRONScan 公开标签或官方披露
--   3. 仅插入 confidence='high' 地址，直接参与净流计算
--   4. TRON 地址大小写敏感，不得 lower()

-- 确保表存在（与 db_stats.py 中 DDL 一致）
CREATE TABLE IF NOT EXISTS biz.onchain_exchange_wallet (
    wallet_id SERIAL PRIMARY KEY,
    address TEXT NOT NULL,
    exchange_name TEXT NOT NULL,
    chain TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'exchange',
    confidence TEXT NOT NULL DEFAULT 'medium',
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_exchange_wallet UNIQUE (address, chain)
);

-- ============================================================
-- BSC：补充至 6 个高可信度种子地址
-- ============================================================
INSERT INTO biz.onchain_exchange_wallet
    (address, exchange_name, chain, label, confidence, source)
VALUES
    -- 已有 3 个（保留）
    ('0x8894e0a0c962cb723c1976a4421c95949be2d4e3', 'Binance', 'bsc', 'exchange', 'high', 'bscscan-label-P0-2'),
    ('0x0d0707963952f2fba59dd06f2b425ace40b492fe', 'Binance', 'bsc', 'exchange', 'high', 'bscscan-label-P0-2'),
    ('0x18b2a687610328590bc8f2e5fedde3b582a49cda', 'Binance', 'bsc', 'exchange', 'medium', 'bscscan-label-P0-2')
    -- 新增 3 个（示例占位符，需替换为真实高可信地址）
    -- TODO: 请女王侧补充真实 BSC 交易所地址；以下为占位，执行前必须替换或删除
    -- ('0x______________________________', 'Binance', 'bsc', 'exchange', 'high', 'bscscan-label-P0-2'),
    -- ('0x______________________________', 'PancakeSwap/Other', 'bsc', 'exchange', 'high', 'bscscan-label-P0-2'),
    -- ('0x______________________________', 'OKX', 'bsc', 'exchange', 'high', 'bscscan-label-P0-2')
ON CONFLICT (address, chain) DO NOTHING;

-- ============================================================
-- TRON：补充 4 个高可信度种子地址
-- ============================================================
-- TRON 地址为 Base58 编码，大小写敏感，请勿修改大小写。
-- 当前为占位符，女王侧需替换为真实地址后执行。
-- 建议来源：TRONScan 公开标签、各交易所官方披露的 TRC-20 充值/冷钱包地址。
--
-- INSERT INTO biz.onchain_exchange_wallet
--     (address, exchange_name, chain, label, confidence, source)
-- VALUES
--     ('T______________________________', 'Binance', 'tron', 'exchange', 'high', 'tronscan-label-P0-2'),
--     ('T______________________________', 'OKX', 'tron', 'exchange', 'high', 'tronscan-label-P0-2'),
--     ('T______________________________', 'Bybit', 'tron', 'exchange', 'high', 'tronscan-label-P0-2'),
--     ('T______________________________', 'KuCoin', 'tron', 'exchange', 'high', 'tronscan-label-P0-2')
-- ON CONFLICT (address, chain) DO NOTHING;

-- 验证：执行后检查各链 high 地址数量
-- SELECT chain, COUNT(*) FROM biz.onchain_exchange_wallet WHERE confidence = 'high' GROUP BY chain;
