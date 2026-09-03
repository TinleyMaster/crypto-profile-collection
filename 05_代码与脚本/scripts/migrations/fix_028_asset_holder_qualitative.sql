-- MEME-06: 定性持仓活跃度兜底表（transfer-log 估算）
CREATE TABLE IF NOT EXISTS biz.asset_holder_qualitative (
    asset_id        BIGINT NOT NULL,
    chain           TEXT  NOT NULL,
    activity_level  VARCHAR(8) NOT NULL,   -- high / mid / low
    tx_n            INTEGER,
    active_addrs    INTEGER,
    cex_in_ratio    NUMERIC,
    source          VARCHAR(16) DEFAULT 'transfer_log',
    computed_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (asset_id, chain)
);
