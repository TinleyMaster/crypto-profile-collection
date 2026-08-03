BEGIN;

-- =========================================================
-- 投研工作流底层数据库重构初始化 SQL
-- Date: 2026-07-29
-- 目标：
-- 1. 按来源分层沉淀 CMC / CoinGecko / DefiLlama
-- 2. 建立统一实体层 asset / protocol / chain
-- 3. 让 biz.coin_basic 退化为消费层映射表
-- 4. 为后续 n8n 工作流重构提供稳定骨架
-- =========================================================

-- ---------------------------------------------------------
-- 0. Schema
-- ---------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS sys;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS src_cmc;
CREATE SCHEMA IF NOT EXISTS src_cg;
CREATE SCHEMA IF NOT EXISTS src_llama;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS biz;

-- ---------------------------------------------------------
-- 1. sys 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys.source_platform (
    platform_code VARCHAR(32) PRIMARY KEY,
    platform_name VARCHAR(128) NOT NULL,
    base_url TEXT,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sys.source_endpoint (
    endpoint_code VARCHAR(64) PRIMARY KEY,
    platform_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    http_method VARCHAR(16) NOT NULL,
    endpoint_path TEXT NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    update_granularity VARCHAR(32),
    is_deprecated BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_source_endpoint_http_method
        CHECK (http_method IN ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'))
);

CREATE TABLE IF NOT EXISTS sys.ingest_run (
    run_id BIGSERIAL PRIMARY KEY,
    platform_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    endpoint_code VARCHAR(64) NOT NULL REFERENCES sys.source_endpoint(endpoint_code),
    workflow_name VARCHAR(128),
    request_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_url TEXT,
    http_status INTEGER,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    total_items INTEGER,
    success_items INTEGER,
    fail_items INTEGER,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER,
    error_message TEXT,
    extra_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_ingest_run_status
        CHECK (status IN ('running', 'success', 'partial_success', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_ingest_run_platform_time
    ON sys.ingest_run (platform_code, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_ingest_run_status_time
    ON sys.ingest_run (status, started_at DESC);

-- ---------------------------------------------------------
-- 2. raw 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.api_response (
    response_id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES sys.ingest_run(run_id) ON DELETE CASCADE,
    platform_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    endpoint_code VARCHAR(64) NOT NULL REFERENCES sys.source_endpoint(endpoint_code),
    request_key TEXT,
    entity_key TEXT,
    page_key TEXT,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_api_response_dedup
    ON raw.api_response (platform_code, endpoint_code, COALESCE(request_key, ''), COALESCE(page_key, ''), payload_hash);

CREATE INDEX IF NOT EXISTS idx_raw_api_response_entity
    ON raw.api_response (platform_code, entity_key, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_api_response_run
    ON raw.api_response (run_id, fetched_at DESC);

-- ---------------------------------------------------------
-- 3. src_cmc 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS src_cmc.cmc_asset_map (
    cmc_id BIGINT PRIMARY KEY,
    symbol VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    slug VARCHAR(256),
    listing_status VARCHAR(32),
    is_active BOOLEAN,
    rank_num INTEGER,
    platform_name VARCHAR(128),
    platform_slug VARCHAR(256),
    platform_symbol VARCHAR(128),
    token_address TEXT,
    first_historical_data TIMESTAMPTZ,
    last_historical_data TIMESTAMPTZ,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cmc_asset_map_symbol
    ON src_cmc.cmc_asset_map (symbol);

CREATE INDEX IF NOT EXISTS idx_cmc_asset_map_slug
    ON src_cmc.cmc_asset_map (slug);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_asset_info (
    cmc_id BIGINT PRIMARY KEY REFERENCES src_cmc.cmc_asset_map(cmc_id) ON DELETE CASCADE,
    description TEXT,
    logo TEXT,
    notice TEXT,
    date_launched DATE,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    urls JSONB NOT NULL DEFAULT '{}'::jsonb,
    platform_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    category_hint VARCHAR(128),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_asset_quote_snapshot (
    cmc_id BIGINT NOT NULL REFERENCES src_cmc.cmc_asset_map(cmc_id) ON DELETE CASCADE,
    quote_time TIMESTAMPTZ NOT NULL,
    price_usd NUMERIC(38, 18),
    market_cap NUMERIC(38, 2),
    fdv NUMERIC(38, 2),
    volume_24h NUMERIC(38, 2),
    circulating_supply NUMERIC(38, 10),
    total_supply NUMERIC(38, 10),
    max_supply NUMERIC(38, 10),
    percent_change_1h NUMERIC(18, 8),
    percent_change_24h NUMERIC(18, 8),
    percent_change_7d NUMERIC(18, 8),
    percent_change_30d NUMERIC(18, 8),
    market_cap_dominance NUMERIC(18, 8),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cmc_id, quote_time)
);

CREATE INDEX IF NOT EXISTS idx_cmc_asset_quote_time
    ON src_cmc.cmc_asset_quote_snapshot (quote_time DESC);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_market_pair_snapshot (
    cmc_id BIGINT NOT NULL REFERENCES src_cmc.cmc_asset_map(cmc_id) ON DELETE CASCADE,
    snapshot_time TIMESTAMPTZ NOT NULL,
    exchange_name VARCHAR(256),
    market_pair TEXT,
    market_type VARCHAR(64),
    category VARCHAR(64),
    pair_base_symbol VARCHAR(128),
    pair_quote_symbol VARCHAR(128),
    price NUMERIC(38, 18),
    volume_24h NUMERIC(38, 2),
    liquidity_usd NUMERIC(38, 2),
    market_url TEXT,
    outlier_score NUMERIC(18, 8),
    effective_liquidity NUMERIC(38, 2),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cmc_id, snapshot_time, market_pair)
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_category (
    category_id VARCHAR(128) PRIMARY KEY,
    category_name VARCHAR(256) NOT NULL,
    title TEXT,
    description TEXT,
    num_tokens INTEGER,
    market_cap NUMERIC(38, 2),
    volume_24h NUMERIC(38, 2),
    last_updated TIMESTAMPTZ,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_category_member (
    category_id VARCHAR(128) NOT NULL REFERENCES src_cmc.cmc_category(category_id) ON DELETE CASCADE,
    cmc_id BIGINT NOT NULL REFERENCES src_cmc.cmc_asset_map(cmc_id) ON DELETE CASCADE,
    snapshot_date DATE NOT NULL,
    rank_in_category INTEGER,
    market_cap NUMERIC(38, 2),
    percent_change_24h NUMERIC(18, 8),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (category_id, cmc_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_dex_token (
    platform_id VARCHAR(64) NOT NULL,
    chain_name VARCHAR(128) NOT NULL,
    token_address TEXT NOT NULL,
    symbol VARCHAR(128),
    name VARCHAR(256),
    decimals INTEGER,
    project_url TEXT,
    logo TEXT,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform_id, token_address)
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_dex_token_price_snapshot (
    platform_id VARCHAR(64) NOT NULL,
    token_address TEXT NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    chain_name VARCHAR(128),
    price_usd NUMERIC(38, 18),
    market_cap NUMERIC(38, 2),
    liquidity_usd NUMERIC(38, 2),
    volume_24h NUMERIC(38, 2),
    price_change_24h NUMERIC(18, 8),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform_id, token_address, snapshot_time),
    FOREIGN KEY (platform_id, token_address)
        REFERENCES src_cmc.cmc_dex_token(platform_id, token_address)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_dex_pool_snapshot (
    platform_id VARCHAR(64) NOT NULL,
    token_address TEXT NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    pool_address TEXT NOT NULL,
    dex_name VARCHAR(256),
    pair_name TEXT,
    liquidity_usd NUMERIC(38, 2),
    volume_24h NUMERIC(38, 2),
    fee_rate NUMERIC(18, 8),
    chain_name VARCHAR(128),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform_id, token_address, snapshot_time, pool_address),
    FOREIGN KEY (platform_id, token_address)
        REFERENCES src_cmc.cmc_dex_token(platform_id, token_address)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS src_cmc.cmc_dex_security_snapshot (
    platform_id VARCHAR(64) NOT NULL,
    token_address TEXT NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    chain_name VARCHAR(128),
    is_honeypot BOOLEAN,
    buy_tax NUMERIC(18, 8),
    sell_tax NUMERIC(18, 8),
    can_take_back_ownership BOOLEAN,
    owner_address TEXT,
    risk_level VARCHAR(32),
    security_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform_id, token_address, snapshot_time),
    FOREIGN KEY (platform_id, token_address)
        REFERENCES src_cmc.cmc_dex_token(platform_id, token_address)
        ON DELETE CASCADE
);

-- ---------------------------------------------------------
-- 4. src_cg 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS src_cg.cg_coin (
    coingecko_id VARCHAR(128) PRIMARY KEY,
    symbol VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    web_slug VARCHAR(256),
    asset_platform_id VARCHAR(128),
    platforms JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cg_coin_symbol
    ON src_cg.cg_coin (symbol);

CREATE TABLE IF NOT EXISTS src_cg.cg_coin_detail (
    coingecko_id VARCHAR(128) PRIMARY KEY REFERENCES src_cg.cg_coin(coingecko_id) ON DELETE CASCADE,
    description TEXT,
    categories JSONB NOT NULL DEFAULT '[]'::jsonb,
    links JSONB NOT NULL DEFAULT '{}'::jsonb,
    image JSONB NOT NULL DEFAULT '{}'::jsonb,
    country_origin VARCHAR(128),
    genesis_date DATE,
    sentiment_votes_up NUMERIC(18, 8),
    sentiment_votes_down NUMERIC(18, 8),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS src_cg.cg_market_snapshot (
    coingecko_id VARCHAR(128) NOT NULL REFERENCES src_cg.cg_coin(coingecko_id) ON DELETE CASCADE,
    snapshot_time TIMESTAMPTZ NOT NULL,
    current_price_usd NUMERIC(38, 18),
    market_cap_usd NUMERIC(38, 2),
    fully_diluted_valuation_usd NUMERIC(38, 2),
    total_volume_usd NUMERIC(38, 2),
    circulating_supply NUMERIC(38, 10),
    total_supply NUMERIC(38, 10),
    max_supply NUMERIC(38, 10),
    ath_usd NUMERIC(38, 18),
    atl_usd NUMERIC(38, 18),
    price_change_percentage_24h NUMERIC(18, 8),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (coingecko_id, snapshot_time)
);

-- ---------------------------------------------------------
-- 5. src_llama 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS src_llama.llama_protocol (
    defillama_slug VARCHAR(256) PRIMARY KEY,
    protocol_name VARCHAR(256) NOT NULL,
    category VARCHAR(128),
    parent_protocol VARCHAR(256),
    url TEXT,
    logo TEXT,
    chains JSONB NOT NULL DEFAULT '[]'::jsonb,
    gecko_id VARCHAR(128),
    cmc_id_hint BIGINT,
    twitter TEXT,
    description TEXT,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llama_protocol_gecko
    ON src_llama.llama_protocol (gecko_id);

CREATE TABLE IF NOT EXISTS src_llama.llama_protocol_metric_daily (
    defillama_slug VARCHAR(256) NOT NULL REFERENCES src_llama.llama_protocol(defillama_slug) ON DELETE CASCADE,
    metric_date DATE NOT NULL,
    tvl NUMERIC(38, 2),
    fees NUMERIC(38, 2),
    revenue NUMERIC(38, 2),
    volume NUMERIC(38, 2),
    stablecoin_tvl NUMERIC(38, 2),
    holders BIGINT,
    extra_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (defillama_slug, metric_date)
);

CREATE TABLE IF NOT EXISTS src_llama.llama_chain (
    llama_chain_slug VARCHAR(128) PRIMARY KEY,
    chain_name VARCHAR(128) NOT NULL,
    chain_type VARCHAR(64),
    gecko_id VARCHAR(128),
    cmc_id_hint BIGINT,
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS src_llama.llama_chain_metric_daily (
    llama_chain_slug VARCHAR(128) NOT NULL REFERENCES src_llama.llama_chain(llama_chain_slug) ON DELETE CASCADE,
    metric_date DATE NOT NULL,
    tvl NUMERIC(38, 2),
    stablecoin_tvl NUMERIC(38, 2),
    bridge_volume NUMERIC(38, 2),
    dex_volume NUMERIC(38, 2),
    fees NUMERIC(38, 2),
    raw_response_id BIGINT REFERENCES raw.api_response(response_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (llama_chain_slug, metric_date)
);

-- ---------------------------------------------------------
-- 6. core 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.asset (
    asset_id BIGSERIAL PRIMARY KEY,
    canonical_symbol VARCHAR(128) NOT NULL,
    canonical_name VARCHAR(256) NOT NULL,
    asset_type VARCHAR(32) NOT NULL DEFAULT 'token',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    launch_date DATE,
    description_short TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_core_asset_type
        CHECK (asset_type IN ('token', 'coin', 'stablecoin', 'lp_token', 'meme', 'synthetic', 'other')),
    CONSTRAINT chk_core_asset_status
        CHECK (status IN ('active', 'inactive', 'deprecated', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_core_asset_symbol
    ON core.asset (canonical_symbol);

CREATE TABLE IF NOT EXISTS core.protocol (
    protocol_id BIGSERIAL PRIMARY KEY,
    canonical_name VARCHAR(256) NOT NULL,
    protocol_type VARCHAR(64),
    official_domain TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    description_short TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_core_protocol_status
        CHECK (status IN ('active', 'inactive', 'deprecated', 'archived'))
);

CREATE TABLE IF NOT EXISTS core.chain (
    chain_id BIGSERIAL PRIMARY KEY,
    chain_name VARCHAR(128) NOT NULL,
    chain_slug VARCHAR(128),
    is_mainnet BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_core_chain_name
    ON core.chain (chain_name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_core_chain_slug
    ON core.chain (chain_slug)
    WHERE chain_slug IS NOT NULL;

CREATE TABLE IF NOT EXISTS core.asset_contract (
    contract_id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    chain_id BIGINT NOT NULL REFERENCES core.chain(chain_id) ON DELETE RESTRICT,
    contract_address TEXT NOT NULL,
    is_native BOOLEAN NOT NULL DEFAULT FALSE,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    decimals INTEGER,
    source_preference VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_asset_contract_source_preference
        CHECK (source_preference IN ('cmc', 'coingecko', 'manual', 'other') OR source_preference IS NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_core_asset_contract_chain_addr
    ON core.asset_contract (chain_id, LOWER(contract_address));

CREATE INDEX IF NOT EXISTS idx_core_asset_contract_asset
    ON core.asset_contract (asset_id);

CREATE TABLE IF NOT EXISTS core.asset_source_map (
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    source_asset_key VARCHAR(256) NOT NULL,
    match_status VARCHAR(32) NOT NULL DEFAULT 'confirmed',
    match_method VARCHAR(32),
    match_confidence NUMERIC(5, 2),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    verified_by VARCHAR(64),
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_code, source_asset_key),
    UNIQUE (asset_id, source_code, source_asset_key),
    CONSTRAINT chk_asset_source_map_status
        CHECK (match_status IN ('candidate', 'confirmed', 'rejected', 'manual_review')),
    CONSTRAINT chk_asset_source_map_confidence
        CHECK ((match_confidence IS NULL) OR (match_confidence >= 0 AND match_confidence <= 100))
);

CREATE INDEX IF NOT EXISTS idx_core_asset_source_map_asset
    ON core.asset_source_map (asset_id, source_code);

CREATE TABLE IF NOT EXISTS core.protocol_source_map (
    protocol_id BIGINT NOT NULL REFERENCES core.protocol(protocol_id) ON DELETE CASCADE,
    source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    source_protocol_key VARCHAR(256) NOT NULL,
    match_status VARCHAR(32) NOT NULL DEFAULT 'confirmed',
    match_method VARCHAR(32),
    match_confidence NUMERIC(5, 2),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    verified_by VARCHAR(64),
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_code, source_protocol_key),
    UNIQUE (protocol_id, source_code, source_protocol_key),
    CONSTRAINT chk_protocol_source_map_status
        CHECK (match_status IN ('candidate', 'confirmed', 'rejected', 'manual_review')),
    CONSTRAINT chk_protocol_source_map_confidence
        CHECK ((match_confidence IS NULL) OR (match_confidence >= 0 AND match_confidence <= 100))
);

CREATE INDEX IF NOT EXISTS idx_core_protocol_source_map_protocol
    ON core.protocol_source_map (protocol_id, source_code);

CREATE TABLE IF NOT EXISTS core.protocol_asset_link (
    protocol_id BIGINT NOT NULL REFERENCES core.protocol(protocol_id) ON DELETE CASCADE,
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL,
    is_primary_token BOOLEAN NOT NULL DEFAULT FALSE,
    confidence_score NUMERIC(5, 2),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (protocol_id, asset_id, relation_type),
    CONSTRAINT chk_protocol_asset_link_relation
        CHECK (relation_type IN ('governance_token', 'gas_token', 'lp_token', 'reward_token', 'wrapped_token', 'other')),
    CONSTRAINT chk_protocol_asset_link_confidence
        CHECK ((confidence_score IS NULL) OR (confidence_score >= 0 AND confidence_score <= 100))
);

CREATE INDEX IF NOT EXISTS idx_core_protocol_asset_link_asset
    ON core.protocol_asset_link (asset_id);

CREATE TABLE IF NOT EXISTS core.mapping_candidate (
    candidate_id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    left_source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    left_source_key VARCHAR(256) NOT NULL,
    right_source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    right_source_key VARCHAR(256) NOT NULL,
    score NUMERIC(5, 2),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    review_note TEXT,
    reviewed_by VARCHAR(64),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_mapping_candidate_entity
        CHECK (entity_type IN ('asset', 'protocol')),
    CONSTRAINT chk_mapping_candidate_score
        CHECK ((score IS NULL) OR (score >= 0 AND score <= 100)),
    CONSTRAINT chk_mapping_candidate_review
        CHECK (review_status IN ('pending', 'approved', 'rejected', 'merged'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_core_mapping_candidate_pair
    ON core.mapping_candidate (entity_type, left_source_code, left_source_key, right_source_code, right_source_key);

-- ---------------------------------------------------------
-- 7. biz 层
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS biz.coin_basic (
    asset_id BIGINT PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    cmc_id BIGINT,
    coingecko_id VARCHAR(128),
    primary_defillama_slug VARCHAR(256),
    coin_symbol VARCHAR(128) NOT NULL,
    coin_name VARCHAR(256) NOT NULL,
    main_chain VARCHAR(128),
    primary_contract_address TEXT,
    official_website TEXT,
    docs_url TEXT,
    github_url TEXT,
    logo_url TEXT,
    description_short TEXT,
    mapping_status VARCHAR(32) NOT NULL DEFAULT 'draft',
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_biz_coin_basic_mapping_status
        CHECK (mapping_status IN ('draft', 'partial', 'confirmed', 'manual_review'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_coin_basic_cmc_id
    ON biz.coin_basic (cmc_id)
    WHERE cmc_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_coin_basic_cg_id
    ON biz.coin_basic (coingecko_id)
    WHERE coingecko_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS biz.research_target (
    asset_id BIGINT PRIMARY KEY REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    target_status VARCHAR(32) NOT NULL DEFAULT '候选',
    priority SMALLINT NOT NULL DEFAULT 3,
    owner VARCHAR(64),
    remark TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_research_target_status
        CHECK (target_status IN ('候选', '跟踪中', '暂停', '归档')),
    CONSTRAINT chk_research_target_priority
        CHECK (priority BETWEEN 1 AND 5)
);

CREATE TABLE IF NOT EXISTS biz.doc_source_entry (
    entry_id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    asset_id BIGINT REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    protocol_id BIGINT REFERENCES core.protocol(protocol_id) ON DELETE CASCADE,
    source_code VARCHAR(32),
    entry_type VARCHAR(32) NOT NULL,
    entry_url TEXT NOT NULL,
    discovered_from VARCHAR(64),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_doc_source_entry_entity
        CHECK (
            (entity_type = 'asset' AND asset_id IS NOT NULL AND protocol_id IS NULL) OR
            (entity_type = 'protocol' AND protocol_id IS NOT NULL AND asset_id IS NULL)
        ),
    CONSTRAINT chk_doc_source_entry_type
        CHECK (entry_type IN ('official_website', 'docs', 'github', 'medium', 'docs_portal', 'whitepaper_page', 'other'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_doc_source_entry_entity_url
    ON biz.doc_source_entry (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url);

CREATE TABLE IF NOT EXISTS biz.doc_asset (
    doc_id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    asset_id BIGINT REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    protocol_id BIGINT REFERENCES core.protocol(protocol_id) ON DELETE CASCADE,
    entry_id BIGINT REFERENCES biz.doc_source_entry(entry_id) ON DELETE SET NULL,
    doc_type VARCHAR(32) NOT NULL,
    source_url TEXT NOT NULL,
    resolved_url TEXT,
    file_name VARCHAR(256),
    mime_type VARCHAR(128),
    content_hash TEXT,
    file_size_bytes BIGINT,
    language VARCHAR(32),
    storage_path TEXT,
    drive_file_url TEXT,
    ima_doc_url TEXT,
    parse_status VARCHAR(32) NOT NULL DEFAULT '待解析',
    sync_status VARCHAR(32) NOT NULL DEFAULT '待同步',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    CONSTRAINT chk_doc_asset_entity
        CHECK (
            (entity_type = 'asset' AND asset_id IS NOT NULL AND protocol_id IS NULL) OR
            (entity_type = 'protocol' AND protocol_id IS NOT NULL AND asset_id IS NULL)
        ),
    CONSTRAINT chk_doc_asset_type
        CHECK (doc_type IN ('whitepaper', 'docs', 'audit', 'deck', 'tokenomics', 'research', 'announcement', 'other')),
    CONSTRAINT chk_doc_asset_parse_status
        CHECK (parse_status IN ('待解析', '已解析', '解析失败', '跳过')),
    CONSTRAINT chk_doc_asset_sync_status
        CHECK (sync_status IN ('待同步', '已同步', '同步失败', '跳过'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_doc_asset_content_hash
    ON biz.doc_asset (content_hash)
    WHERE content_hash IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_biz_doc_asset_entity_source
    ON biz.doc_asset (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), source_url);

CREATE TABLE IF NOT EXISTS biz.asset_market_daily (
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    market_date DATE NOT NULL,
    source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    price_usd NUMERIC(38, 18),
    market_cap NUMERIC(38, 2),
    fdv NUMERIC(38, 2),
    circulating_supply NUMERIC(38, 10),
    total_supply NUMERIC(38, 10),
    volume_24h NUMERIC(38, 2),
    change_24h NUMERIC(18, 8),
    change_7d NUMERIC(18, 8),
    raw_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, market_date, source_code)
);

CREATE TABLE IF NOT EXISTS biz.protocol_metric_daily (
    protocol_id BIGINT NOT NULL REFERENCES core.protocol(protocol_id) ON DELETE CASCADE,
    metric_date DATE NOT NULL,
    source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    tvl NUMERIC(38, 2),
    fees NUMERIC(38, 2),
    revenue NUMERIC(38, 2),
    volume NUMERIC(38, 2),
    users_count BIGINT,
    raw_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (protocol_id, metric_date, source_code)
);

CREATE TABLE IF NOT EXISTS biz.asset_unlock_event (
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    unlock_date DATE NOT NULL,
    unlock_type VARCHAR(32) NOT NULL,
    source_code VARCHAR(32) NOT NULL REFERENCES sys.source_platform(platform_code),
    unlock_amount NUMERIC(38, 10),
    unlock_ratio_total NUMERIC(18, 8),
    unlock_ratio_circulating NUMERIC(18, 8),
    unlock_value_usd NUMERIC(38, 2),
    beneficiary_type VARCHAR(64),
    remaining_locked NUMERIC(38, 10),
    risk_level VARCHAR(32),
    raw_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, unlock_date, unlock_type, source_code),
    CONSTRAINT chk_asset_unlock_event_risk
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical') OR risk_level IS NULL)
);

-- ---------------------------------------------------------
-- 8. 通用索引
-- ---------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_biz_coin_basic_symbol
    ON biz.coin_basic (coin_symbol);

CREATE INDEX IF NOT EXISTS idx_biz_research_target_status
    ON biz.research_target (target_status, priority);

CREATE INDEX IF NOT EXISTS idx_biz_doc_asset_updated
    ON biz.doc_asset (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_biz_asset_market_daily_date
    ON biz.asset_market_daily (market_date DESC, source_code);

CREATE INDEX IF NOT EXISTS idx_biz_protocol_metric_daily_date
    ON biz.protocol_metric_daily (metric_date DESC, source_code);

CREATE INDEX IF NOT EXISTS idx_biz_asset_unlock_event_date
    ON biz.asset_unlock_event (unlock_date DESC, source_code);

-- ---------------------------------------------------------
-- 9. Seed 基础来源配置
-- ---------------------------------------------------------
INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description)
VALUES
    ('cmc', 'CoinMarketCap', 'https://pro-api.coinmarketcap.com', '中心化资产与 DEX Token 数据源'),
    ('coingecko', 'CoinGecko', 'https://api.coingecko.com/api/v3', '资产基础信息与行情补充数据源'),
    ('defillama', 'DefiLlama', 'https://api.llama.fi', '协议、链与 DeFi 指标数据源'),
    ('manual', 'Manual', NULL, '人工维护与人工修正来源')
ON CONFLICT (platform_code) DO UPDATE SET
    platform_name = EXCLUDED.platform_name,
    base_url = EXCLUDED.base_url,
    description = EXCLUDED.description,
    updated_at = NOW();

INSERT INTO sys.source_endpoint (endpoint_code, platform_code, http_method, endpoint_path, entity_type, update_granularity, is_deprecated, notes)
VALUES
    ('cmc_map', 'cmc', 'GET', '/v1/cryptocurrency/map', 'asset', 'full_sync', FALSE, 'CMC 全市场币种目录'),
    ('cmc_info', 'cmc', 'GET', '/v2/cryptocurrency/info', 'asset', 'batch_refresh', FALSE, 'CMC 币种详情'),
    ('cmc_quotes_latest', 'cmc', 'GET', '/v3/cryptocurrency/quotes/latest', 'asset_market', 'intraday', FALSE, 'CMC 最新行情'),
    ('cmc_market_pairs', 'cmc', 'GET', '/v2/cryptocurrency/market-pairs/latest', 'asset_market', 'intraday', FALSE, 'CMC 交易对快照'),
    ('cmc_categories', 'cmc', 'GET', '/v1/cryptocurrency/categories', 'category', 'daily', FALSE, 'CMC 类别列表'),
    ('cmc_category', 'cmc', 'GET', '/v1/cryptocurrency/category', 'category_member', 'daily', FALSE, 'CMC 类别成员'),
    ('cmc_dex_token', 'cmc', 'GET', '/v1/dex/token', 'dex_token', 'on_demand', FALSE, 'CMC DEX Token 详情'),
    ('cmc_dex_token_price', 'cmc', 'GET', '/v1/dex/token/price', 'dex_token_market', 'intraday', FALSE, 'CMC DEX Token 价格'),
    ('cmc_dex_token_pools', 'cmc', 'GET', '/v1/dex/token/pools', 'dex_pool', 'intraday', FALSE, 'CMC DEX Token 池子'),
    ('cmc_dex_security', 'cmc', 'GET', '/v1/dex/security/detail', 'dex_security', 'intraday', FALSE, 'CMC DEX Token 风险'),
    ('cg_coin', 'coingecko', 'GET', '/coins/{id}', 'asset', 'batch_refresh', FALSE, 'CoinGecko 币种详情'),
    ('cg_markets', 'coingecko', 'GET', '/coins/markets', 'asset_market', 'intraday', FALSE, 'CoinGecko 市场快照'),
    ('llama_protocols', 'defillama', 'GET', '/protocols', 'protocol', 'daily', FALSE, 'DefiLlama 协议目录'),
    ('llama_protocol', 'defillama', 'GET', '/protocol/{slug}', 'protocol_metric', 'daily', FALSE, 'DefiLlama 单协议详情'),
    ('llama_chains', 'defillama', 'GET', '/v2/chains', 'chain', 'daily', FALSE, 'DefiLlama 链列表')
ON CONFLICT (endpoint_code) DO UPDATE SET
    platform_code = EXCLUDED.platform_code,
    http_method = EXCLUDED.http_method,
    endpoint_path = EXCLUDED.endpoint_path,
    entity_type = EXCLUDED.entity_type,
    update_granularity = EXCLUDED.update_granularity,
    is_deprecated = EXCLUDED.is_deprecated,
    notes = EXCLUDED.notes,
    updated_at = NOW();

COMMIT;
