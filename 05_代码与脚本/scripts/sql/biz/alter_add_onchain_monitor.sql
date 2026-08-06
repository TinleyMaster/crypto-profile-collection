-- Phase 1: 链上数据监控
-- 持仓快照表 + 大额转账日志表 + 交易所钱包地址库

-- ============================================================
-- 1. 持仓快照表
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.onchain_holder_snapshot (
    snapshot_id      SERIAL PRIMARY KEY,
    asset_id         INTEGER NOT NULL,
    chain            TEXT   NOT NULL,       -- eth / bsc / solana
    contract_address TEXT   NOT NULL,       -- 代币合约地址
    snapshot_date    DATE   NOT NULL,       -- 快照日期

    -- 持仓集中度
    top10_concentration  NUMERIC(5,2),      -- Top 10 持仓占比 (%)
    top50_concentration  NUMERIC(5,2),      -- Top 50 持仓占比 (%)
    top100_concentration NUMERIC(5,2),      -- Top 100 持仓占比 (%)

    -- Holder 数据
    total_holders        INTEGER,           -- 独立持币地址数
    holder_change_7d     INTEGER,           -- 7 日地址变化
    holder_change_30d    INTEGER,           -- 30 日地址变化

    -- 巨鲸动向
    whale_balance_change_7d_pct  NUMERIC(6,2),  -- 巨鲸 7 日持仓变化 (%)
    whale_balance_change_30d_pct NUMERIC(6,2),  -- 巨鲸 30 日持仓变化 (%)

    -- 地址类型分布 (基于标签)
    exchange_wallet_pct   NUMERIC(5,2),     -- 交易所钱包持仓占比 (%)
    vc_wallet_pct         NUMERIC(5,2),     -- VC 机构持仓占比 (%)
    smart_money_pct       NUMERIC(5,2),     -- 聪明钱持仓占比 (%)
    retail_pct            NUMERIC(5,2),     -- 散户持仓占比 (%)
    contract_pct          NUMERIC(5,2),     -- 合约地址持仓占比 (%)

    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_holder_snapshot_asset
        FOREIGN KEY (asset_id) REFERENCES core.asset(asset_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE biz.onchain_holder_snapshot IS
    '持仓快照：Top 持有者集中度、地址类型分布、巨鲸动向。每日更新。';

CREATE INDEX IF NOT EXISTS idx_holder_snapshot_asset_date
    ON biz.onchain_holder_snapshot (asset_id, snapshot_date DESC);

-- ============================================================
-- 2. 大额转账日志表
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.onchain_transfer_log (
    log_id           SERIAL PRIMARY KEY,
    asset_id         INTEGER,
    chain            TEXT   NOT NULL,       -- eth / bsc / solana
    contract_address TEXT   NOT NULL,       -- 代币合约地址
    tx_hash          TEXT   NOT NULL,       -- 交易哈希

    from_address     TEXT   NOT NULL,
    to_address       TEXT   NOT NULL,
    value            NUMERIC NOT NULL,       -- 转账数量（代币单位）
    value_usd        NUMERIC(15,2),          -- 转账金额（美元）

    from_label       TEXT,                   -- 发送方标签（exchange/vc/whale/unknown）
    to_label         TEXT,                   -- 接收方标签
    from_exchange    TEXT,                   -- 发送方交易所名称
    to_exchange      TEXT,                   -- 接收方交易所名称

    block_number     INTEGER,
    block_timestamp  TIMESTAMPTZ,            -- 区块时间戳

    is_to_exchange   BOOLEAN DEFAULT FALSE,  -- 是否转入交易所（潜在砸盘信号）
    alert_sent_at    TIMESTAMPTZ,            -- 告警发送时间

    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_onchain_tx UNIQUE (chain, tx_hash, contract_address, from_address, to_address)
);

COMMENT ON TABLE biz.onchain_transfer_log IS
    '大额转账日志：监控单笔大额转入交易所、跨链转出、巨鲸互转等异动。';

CREATE INDEX IF NOT EXISTS idx_transfer_log_asset_time
    ON biz.onchain_transfer_log (asset_id, block_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_transfer_log_to_exchange
    ON biz.onchain_transfer_log (is_to_exchange, block_timestamp DESC)
    WHERE is_to_exchange = TRUE;

CREATE INDEX IF NOT EXISTS idx_transfer_log_chain_contract
    ON biz.onchain_transfer_log (chain, contract_address, block_timestamp DESC);

-- ============================================================
-- 3. 交易所钱包地址库
-- ============================================================
CREATE TABLE IF NOT EXISTS biz.onchain_exchange_wallet (
    wallet_id      SERIAL PRIMARY KEY,
    address        TEXT   NOT NULL,          -- 钱包地址
    exchange_name  TEXT   NOT NULL,          -- 交易所名称（Binance/Coinbase/OKX/...）
    chain          TEXT   NOT NULL,          -- 所属链
    label          TEXT   DEFAULT 'exchange',-- 标签类型
    confidence     TEXT   DEFAULT 'high',    -- 可信度：high/medium/low
    source         TEXT   DEFAULT 'manual',  -- 数据来源
    added_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_exchange_wallet UNIQUE (address, chain)
);

COMMENT ON TABLE biz.onchain_exchange_wallet IS
    '交易所钱包地址库：用于标注转账的发送方/接收方是否为交易所，判断砸盘风险。';

CREATE INDEX IF NOT EXISTS idx_exchange_wallet_address
    ON biz.onchain_exchange_wallet (address, chain);