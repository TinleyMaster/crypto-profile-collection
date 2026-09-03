-- MEME-07: Solana 钱包聚类 insider 网络表（RugCheck /report 端点）
CREATE TABLE IF NOT EXISTS biz.asset_insider_clusters (
    asset_id                  BIGINT PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    chain                     VARCHAR(16),
    mint                      TEXT,
    graph_insiders_detected   INT,
    insider_network_count     INT,
    top_network_size          INT,
    top_network_active_accounts INT,
    top_network_token_amount  NUMERIC,
    total_supply             NUMERIC,
    total_holders            INT,
    insider_dominance         NUMERIC(6,4),
    insider_account_ratio     NUMERIC(6,4),
    bundle_flag               BOOLEAN,
    risk_label                VARCHAR(16),
    networks_json             JSONB,
    source                    VARCHAR(16),
    source_status             VARCHAR(16),
    raw_json                  JSONB,
    scanned_at                TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aic_chain ON biz.asset_insider_clusters(chain);
CREATE INDEX IF NOT EXISTS idx_aic_risk  ON biz.asset_insider_clusters(risk_label);
