-- P0-3 通用地址标签表：从交易所扩展到所有类型（聪明钱 / 巨鲸 / MEV / 做市商 / DEX 等）
-- 风险：新建 biz.onchain_address_label，不改动现有表，无破坏性
-- 执行方式：psql $DATABASE_URL -f 05_代码与脚本/scripts/migrations/fix_033_create_address_label_table.sql
--
-- 设计原则：
--   1. 幂等：CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
--   2. 泛化：不限定 exchange，支持任意 label_type
--   3. 多标签：同一地址可有多条不同类型标签（UNIQUE 含 label_type + label_name）
--   4. 可溯源：source / first_seen_tx / raw_meta 保留来源证据
--   5. 置信度分级：high / medium / low，与 onchain_exchange_wallet 一致

CREATE TABLE IF NOT EXISTS biz.onchain_address_label (
    label_id        SERIAL PRIMARY KEY,
    address         TEXT        NOT NULL,
    chain           TEXT        NOT NULL,
    label_type      TEXT        NOT NULL,   -- exchange / smart_money / whale / mev_bot /
                                            -- market_maker / dex / bridge / project_team /
                                            -- relay_solver / other
    label_name      TEXT        NOT NULL,   -- 原始标签名，如 "Binance 14" / "Relay: Solver"
    display_name    TEXT,                   -- 前端展示简称，如 "Binance"（可选）
    confidence      TEXT        NOT NULL DEFAULT 'low',  -- high / medium / low
    source          TEXT,                   -- auto_bscscan_csv / auto_ethplorer / community / ...
    raw_meta        JSONB,                  -- 原始元数据（余额、标签页信息等，可选）
    first_seen_tx   TEXT,                   -- 首次在大额转账中出现的 tx_hash（溯源）
    first_seen_at   TIMESTAMPTZ,            -- 首次出现时间
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_address_label UNIQUE (address, chain, label_type, label_name)
);

-- 按地址查标签（大额转账打标用，高频）
CREATE INDEX IF NOT EXISTS idx_addr_label_addr_chain
    ON biz.onchain_address_label (address, chain);

-- 按类型 + 链批量取（如取所有 exchange 地址做净流）
CREATE INDEX IF NOT EXISTS idx_addr_label_type_chain
    ON biz.onchain_address_label (chain, label_type);

-- 按置信度过滤（只取 high 参与计算）
CREATE INDEX IF NOT EXISTS idx_addr_label_confidence
    ON biz.onchain_address_label (confidence);

-- 验证：建表后检查
-- SELECT label_type, confidence, COUNT(*) FROM biz.onchain_address_label GROUP BY label_type, confidence ORDER BY COUNT(*) DESC;
