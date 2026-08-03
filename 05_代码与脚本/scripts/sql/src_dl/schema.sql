-- DefiLlama source schema
CREATE SCHEMA IF NOT EXISTS src_dl;

-- Protocol list from /protocols (with TVL snapshots)
CREATE TABLE IF NOT EXISTS src_dl.protocol_list (
    protocol_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    slug TEXT NOT NULL,
    category TEXT,
    chain TEXT,
    chains JSONB,
    tvl NUMERIC(20, 2),
    change_1h NUMERIC,
    change_1d NUMERIC,
    change_7d NUMERIC,
    url TEXT,
    description TEXT,
    address TEXT,
    twitter TEXT,
    cmc_id TEXT,
    gecko_id TEXT,
    raw_response_id INTEGER,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_dl_protocol_list_symbol ON src_dl.protocol_list(symbol);
CREATE INDEX IF NOT EXISTS idx_dl_protocol_list_cmc_id ON src_dl.protocol_list(cmc_id);
CREATE INDEX IF NOT EXISTS idx_dl_protocol_list_gecko_id ON src_dl.protocol_list(gecko_id);
CREATE INDEX IF NOT EXISTS idx_dl_protocol_list_slug ON src_dl.protocol_list(slug);
CREATE INDEX IF NOT EXISTS idx_dl_protocol_list_tvl ON src_dl.protocol_list(tvl DESC);
