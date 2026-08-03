-- CoinGecko source schema
CREATE SCHEMA IF NOT EXISTS src_cg;

-- Coin list from /coins/list
CREATE TABLE IF NOT EXISTS src_cg.coin_list (
    coin_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    platforms JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Coin detail from /coins/{id}
CREATE TABLE IF NOT EXISTS src_cg.coin_info (
    coin_id TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    description TEXT,
    homepage_url TEXT,
    image TEXT,
    genesis_date TEXT,
    market_cap_rank INTEGER,
    coingecko_rank INTEGER,
    categories JSONB,
    platforms JSONB,
    links JSONB,
    raw_response_id INTEGER,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_coin_list_symbol ON src_cg.coin_list(symbol);
CREATE INDEX IF NOT EXISTS idx_coin_list_name ON src_cg.coin_list(name);
CREATE INDEX IF NOT EXISTS idx_coin_info_symbol ON src_cg.coin_info(symbol);
CREATE INDEX IF NOT EXISTS idx_coin_info_name ON src_cg.coin_info(name);
