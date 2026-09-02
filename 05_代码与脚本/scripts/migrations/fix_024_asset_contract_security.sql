-- fix_024: 合约安全扫描宽表
-- 对应工单 MEME-02（GoPlus EVM + RugCheck Solana + SolanaClient 兜底）

BEGIN;

CREATE TABLE IF NOT EXISTS biz.asset_contract_security (
    asset_id          BIGINT PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    chain             VARCHAR(16),
    contract_addr     TEXT,
    source            VARCHAR(16),          -- goplus / rugcheck / solana_rpc
    source_status     VARCHAR(16),          -- hit / not_cached / error / na
    is_honeypot       BOOLEAN,
    is_open_source    BOOLEAN,
    is_mintable       BOOLEAN,
    can_take_back_ownership BOOLEAN,
    hidden_owner      BOOLEAN,
    is_blacklisted    BOOLEAN,
    freeze_authority  TEXT,                 -- null = 已放弃冻结
    mint_authority    TEXT,                 -- null = 已放弃增发
    buy_tax           NUMERIC(8,4),
    sell_tax          NUMERIC(8,4),
    lp_locked_pct     NUMERIC(6,2),
    top_holders_pct   NUMERIC(6,2),
    holder_count      INT,
    creator_percent   NUMERIC(6,2),
    risk_score        NUMERIC(6,2),
    raw_json          JSONB,
    scanned_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acs_chain ON biz.asset_contract_security(chain);
CREATE INDEX IF NOT EXISTS idx_acs_status ON biz.asset_contract_security(source_status);

COMMIT;
