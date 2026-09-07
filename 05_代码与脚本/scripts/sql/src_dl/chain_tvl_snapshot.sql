-- Chain TVL daily snapshot from DeFiLlama /v2/chains + /v2/historicalChainTvl
-- 每天一条记录，存每条链的 TVL 快照及变化率，用于计算资金净流入
CREATE TABLE IF NOT EXISTS src_dl.chain_tvl_snapshot (
    chain_key           TEXT            NOT NULL,   -- DeFiLlama 链名（小写，用作主键）
    chain_name          TEXT            NOT NULL,   -- 原始显示名
    snapshot_date       DATE            NOT NULL,   -- 快照日期（UTC）
    tvl_usd             NUMERIC(30, 2),             -- 当日 TVL（USD）
    tvl_change_1d       NUMERIC,                    -- 1日变化率（%），从 DeFiLlama 直接给的字段或自己算
    tvl_change_7d       NUMERIC,                    -- 7日变化率（%）
    tvl_change_30d      NUMERIC,                    -- 30日变化率（%）
    flow_1d_usd         NUMERIC(30, 2),             -- 1日净流入（USD）= tvl - tvl_prev_1d
    flow_7d_usd         NUMERIC(30, 2),             -- 7日净流入（USD）
    flow_30d_usd        NUMERIC(30, 2),             -- 30日净流入（USD）
    raw_response_id     INTEGER,                    -- 关联 raw.api_response
    fetched_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain_key, snapshot_date)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_dl_chain_tvl_snapshot_date ON src_dl.chain_tvl_snapshot(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_dl_chain_tvl_snapshot_tvl ON src_dl.chain_tvl_snapshot(snapshot_date, tvl_usd DESC);
