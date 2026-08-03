BEGIN;

-- ============================================
-- Zeabur Postgres MVP schema for crypto research
-- Covers:
-- 1) asset_master
-- 2) market_snapshots
-- 3) onchain_snapshots
-- 4) source_documents
-- 5) research_alerts
-- ============================================

CREATE TABLE IF NOT EXISTS asset_master (
    asset_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    project_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    chain_type TEXT,
    contract_addresses JSONB NOT NULL DEFAULT '[]'::jsonb,
    official_urls JSONB NOT NULL DEFAULT '{}'::jsonb,
    sector_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    research_priority TEXT NOT NULL DEFAULT 'B',
    research_bucket TEXT NOT NULL DEFAULT 'watchlist',
    mapping_confidence TEXT NOT NULL DEFAULT 'high',
    mapping_review_status TEXT NOT NULL DEFAULT 'approved',
    liquidity_tier TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    base_case TEXT,
    risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_seen_in_report TEXT,
    report_coverage_count INTEGER NOT NULL DEFAULT 1,
    cmc_id TEXT,
    coingecko_id TEXT,
    dune_namespace TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_asset_master_research_priority
        CHECK (research_priority IN ('S', 'A', 'B', 'C')),
    CONSTRAINT chk_asset_master_research_bucket
        CHECK (research_bucket IN ('core', 'watchlist', 'mapping_risk')),
    CONSTRAINT chk_asset_master_mapping_confidence
        CHECK (mapping_confidence IN ('high', 'medium', 'low')),
    CONSTRAINT chk_asset_master_mapping_review_status
        CHECK (mapping_review_status IN ('approved', 'pending', 'manual_review_required', 'rejected')),
    CONSTRAINT chk_asset_master_status
        CHECK (status IN ('active', 'watchlist', 'deprecated', 'archived')),
    CONSTRAINT chk_asset_master_report_coverage_count
        CHECK (report_coverage_count >= 0)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES asset_master(asset_id) ON DELETE CASCADE,
    snapshot_time TIMESTAMPTZ NOT NULL,
    source_name TEXT NOT NULL,
    source_record_id TEXT,
    price_usd NUMERIC(30, 10),
    market_cap_usd NUMERIC(30, 2),
    volume_24h_usd NUMERIC(30, 2),
    fdv_usd NUMERIC(30, 2),
    circulating_supply NUMERIC(38, 10),
    total_supply NUMERIC(38, 10),
    max_supply NUMERIC(38, 10),
    change_1h_pct NUMERIC(12, 6),
    change_24h_pct NUMERIC(12, 6),
    change_7d_pct NUMERIC(12, 6),
    change_30d_pct NUMERIC(12, 6),
    market_rank INTEGER,
    liquidity_tier TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_market_snapshots_asset_time_source
        UNIQUE (asset_id, snapshot_time, source_name)
);

CREATE TABLE IF NOT EXISTS onchain_snapshots (
    id BIGSERIAL PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES asset_master(asset_id) ON DELETE CASCADE,
    snapshot_time TIMESTAMPTZ NOT NULL,
    source_name TEXT NOT NULL,
    chain_name TEXT NOT NULL,
    coverage_type TEXT,
    data_coverage_note TEXT,
    active_addresses_24h BIGINT,
    transfer_count_24h BIGINT,
    transfer_volume_usd_24h NUMERIC(30, 2),
    whale_tx_count_24h BIGINT,
    whale_volume_usd_24h NUMERIC(30, 2),
    dex_trades_24h BIGINT,
    dex_traders_24h BIGINT,
    dex_volume_usd_24h NUMERIC(30, 2),
    holder_count BIGINT,
    top10_holder_ratio NUMERIC(12, 6),
    daily_active_users BIGINT,
    new_addresses_24h BIGINT,
    retention_7d_pct NUMERIC(12, 6),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_onchain_snapshots_asset_time_source_chain
        UNIQUE (asset_id, snapshot_time, source_name, chain_name)
);

CREATE TABLE IF NOT EXISTS source_documents (
    id BIGSERIAL PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES asset_master(asset_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT,
    published_at TIMESTAMPTZ,
    language_code TEXT,
    is_official BOOLEAN NOT NULL DEFAULT FALSE,
    author_name TEXT,
    content_hash TEXT,
    storage_url TEXT,
    raw_text_excerpt TEXT,
    cleaned_summary TEXT,
    quality_score NUMERIC(6, 2),
    relevance_score NUMERIC(6, 2),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_source_documents_source_type
        CHECK (
            source_type IN (
                'website',
                'docs',
                'whitepaper',
                'blog',
                'github',
                'governance',
                'news',
                'social',
                'research',
                'report',
                'pdf',
                'other'
            )
        )
);

CREATE TABLE IF NOT EXISTS research_alerts (
    id BIGSERIAL PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES asset_master(asset_id) ON DELETE CASCADE,
    alert_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT,
    trigger_reason TEXT NOT NULL,
    supporting_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    suggested_action TEXT,
    signal_score NUMERIC(8, 2),
    source_snapshot_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    workflow_run_id TEXT,
    dedup_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_research_alerts_severity
        CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT chk_research_alerts_status
        CHECK (status IN ('open', 'acknowledged', 'dismissed', 'resolved'))
);

CREATE INDEX IF NOT EXISTS idx_asset_master_priority_bucket
    ON asset_master (research_priority, research_bucket);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_asset_time
    ON market_snapshots (asset_id, snapshot_time DESC);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_source_time
    ON market_snapshots (source_name, snapshot_time DESC);

CREATE INDEX IF NOT EXISTS idx_onchain_snapshots_asset_time
    ON onchain_snapshots (asset_id, snapshot_time DESC);

CREATE INDEX IF NOT EXISTS idx_onchain_snapshots_chain_time
    ON onchain_snapshots (chain_name, snapshot_time DESC);

CREATE INDEX IF NOT EXISTS idx_source_documents_asset_type
    ON source_documents (asset_id, source_type);

CREATE INDEX IF NOT EXISTS idx_source_documents_published_at
    ON source_documents (published_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_documents_asset_source_url
    ON source_documents (asset_id, source_url);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_documents_asset_content_hash
    ON source_documents (asset_id, content_hash)
    WHERE content_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_research_alerts_asset_time
    ON research_alerts (asset_id, alert_time DESC);

CREATE INDEX IF NOT EXISTS idx_research_alerts_status_severity
    ON research_alerts (status, severity, alert_time DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_research_alerts_dedup_key
    ON research_alerts (dedup_key)
    WHERE dedup_key IS NOT NULL;

-- ============================================
-- Seed data for the first asset_master pool.
-- These rows are aligned with existing reports
-- and can be edited later after manual review.
-- ============================================

INSERT INTO asset_master (
    asset_id,
    symbol,
    project_name,
    aliases,
    chain_type,
    sector_tags,
    research_priority,
    research_bucket,
    mapping_confidence,
    mapping_review_status,
    liquidity_tier,
    status,
    base_case,
    risk_flags,
    first_seen_in_report,
    report_coverage_count,
    notes
)
VALUES
    (
        'spcx',
        'SPCX',
        'SpaceX tokenized stock (Backpack)',
        '["SpaceX tokenized stock", "SPCX"]'::jsonb,
        'Solana',
        '["RWA", "tokenized_equity"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'mid',
        'active',
        'Tokenized equity proxy for SpaceX narrative and 24/7 onchain trading.',
        '["short_history", "event_driven", "high_speculation"]'::jsonb,
        '2026-06-15 SPCX综合分析报告.md',
        1,
        'Backpack tokenized stock mapping already appears in report and includes contract context.'
    ),
    (
        'near',
        'NEAR',
        'NEAR Protocol',
        '["NEAR", "NEAR Protocol"]'::jsonb,
        'Native L1',
        '["AI", "L1", "chain_abstraction"]'::jsonb,
        'S',
        'core',
        'high',
        'approved',
        'high',
        'active',
        'AI plus chain abstraction infrastructure with improving attention and recurring research value.',
        '["keyword_noise"]'::jsonb,
        '2026-06-16 NEAR、TAO、WLD、HYPE、ONDO、LINK 六币综合分析报告.md',
        3,
        'Tracked repeatedly across multi-asset reports.'
    ),
    (
        'tao',
        'TAO',
        'Bittensor',
        '["TAO", "Bittensor"]'::jsonb,
        'Native L1',
        '["AI", "subnet", "infrastructure"]'::jsonb,
        'S',
        'core',
        'high',
        'approved',
        'high',
        'active',
        'AI subnet economy beta with strong narrative sensitivity.',
        '["keyword_noise", "native_chain_coverage_gap"]'::jsonb,
        '2026-06-16 NEAR、TAO、WLD、HYPE、ONDO、LINK 六币综合分析报告.md',
        3,
        'Needs chain-specific onchain adapter because Dune coverage is not always directly comparable.'
    ),
    (
        'wld',
        'WLD',
        'Worldcoin',
        '["WLD", "Worldcoin", "World ID"]'::jsonb,
        'EVM',
        '["identity", "DID", "AI"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'high',
        'active',
        'Identity network with strong distribution narrative and high retail noise.',
        '["social_noise", "reward_post_noise"]'::jsonb,
        '2026-06-16 NEAR、TAO、WLD、HYPE、ONDO、LINK 六币综合分析报告.md',
        3,
        'Covered both in multi-asset reports and in Billions vs Worldcoin comparative research.'
    ),
    (
        'hype',
        'HYPE',
        'Hyperliquid',
        '["HYPE", "Hyperliquid"]'::jsonb,
        'Native L1',
        '["trading_infra", "perps", "exchange"]'::jsonb,
        'S',
        'core',
        'high',
        'approved',
        'high',
        'active',
        'Trading infrastructure asset with strong trend behavior and platform-level thesis.',
        '["high_beta", "trend_reversal_risk"]'::jsonb,
        '2026-06-16 NEAR、TAO、WLD、HYPE、ONDO、LINK 六币综合分析报告.md',
        2,
        'Should be updated at higher frequency than average watchlist assets.'
    ),
    (
        'ondo',
        'ONDO',
        'Ondo',
        '["ONDO", "Ondo", "Ondo Finance"]'::jsonb,
        'EVM',
        '["RWA", "tokenized_finance"]'::jsonb,
        'S',
        'core',
        'high',
        'approved',
        'high',
        'active',
        'Core RWA and tokenized finance asset for medium-term tracking.',
        '["narrative_hot_but_search_cool"]'::jsonb,
        '2026-06-16 NEAR、TAO、WLD、HYPE、ONDO、LINK 六币综合分析报告.md',
        3,
        'One of the highest-priority assets for website homepage and alerting.'
    ),
    (
        'link',
        'LINK',
        'Chainlink',
        '["LINK", "Chainlink", "CCIP"]'::jsonb,
        'EVM',
        '["oracle", "CCIP", "tokenization"]'::jsonb,
        'S',
        'core',
        'high',
        'approved',
        'high',
        'active',
        'Infrastructure value asset for tokenization, CCIP and oracle demand.',
        '["keyword_noise"]'::jsonb,
        '2026-06-16 NEAR、TAO、WLD、HYPE、ONDO、LINK 六币综合分析报告.md',
        2,
        'Often stronger on fundamentals and onchain usage than on search attention.'
    ),
    (
        'sol',
        'SOL',
        'Solana',
        '["SOL", "Solana"]'::jsonb,
        'Native L1',
        '["L1", "high_beta", "ecosystem"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'high',
        'active',
        'Large-cap high beta chain used as benchmark and rotation proxy.',
        '[]'::jsonb,
        '2026-06-18 SOL、XRP、HYPE、ONDO、SUI、NEAR、TAO、AVAX、LINK 九币综合分析报告.md',
        1,
        'Useful benchmark asset in multi-asset comparisons.'
    ),
    (
        'xrp',
        'XRP',
        'XRP',
        '["XRP", "Ripple", "Ripple payments"]'::jsonb,
        'Native L1',
        '["payments", "compliance", "large_cap"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'high',
        'active',
        'Large-cap compliance and payments narrative asset.',
        '["event_driven"]'::jsonb,
        '2026-06-18 SOL、XRP、HYPE、ONDO、SUI、NEAR、TAO、AVAX、LINK 九币综合分析报告.md',
        1,
        'Should be treated as a narrative benchmark rather than a niche alpha source.'
    ),
    (
        'sui',
        'SUI',
        'Sui',
        '["SUI", "Sui"]'::jsonb,
        'Native L1',
        '["L1", "ecosystem"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'high',
        'active',
        'Newer L1 observation asset for rotation and relative strength monitoring.',
        '[]'::jsonb,
        '2026-06-18 SOL、XRP、HYPE、ONDO、SUI、NEAR、TAO、AVAX、LINK 九币综合分析报告.md',
        1,
        'Keep at medium frequency unless it moves into core thesis.'
    ),
    (
        'avax',
        'AVAX',
        'Avalanche',
        '["AVAX", "Avalanche"]'::jsonb,
        'Native L1',
        '["L1", "subnets", "institutional"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'high',
        'active',
        'Institution-friendly chain with subnet and asset tokenization angle.',
        '[]'::jsonb,
        '2026-06-18 SOL、XRP、HYPE、ONDO、SUI、NEAR、TAO、AVAX、LINK 九币综合分析报告.md',
        1,
        'Better as a structural watchlist asset than a high-frequency alpha source.'
    ),
    (
        'eigen',
        'EIGEN',
        'EigenCloud',
        '["EIGEN", "EigenCloud", "EigenLayer"]'::jsonb,
        'EVM',
        '["infrastructure", "restaking", "high_beta"]'::jsonb,
        'A',
        'watchlist',
        'high',
        'approved',
        'mid',
        'active',
        'Repair-trend high beta asset with meaningful short-term monitoring value.',
        '["keyword_noise"]'::jsonb,
        '2026-06-23 STRC、W、NEAR、EIGEN、ONDO、TAO 多币综合分析报告.md',
        1,
        'Useful in divergence scanning and short-term rotation tracking.'
    ),
    (
        'wormhole',
        'W',
        'Wormhole',
        '["W", "Wormhole"]'::jsonb,
        'Multi-chain',
        '["bridge", "interoperability"]'::jsonb,
        'B',
        'mapping_risk',
        'medium',
        'manual_review_required',
        'mid',
        'watchlist',
        'Interoperability asset with ticker ambiguity risk if automated only by symbol.',
        '["ticker_ambiguity"]'::jsonb,
        '2026-06-23 STRC、W、NEAR、EIGEN、ONDO、TAO 多币综合分析报告.md',
        1,
        'Use asset_id wormhole internally and avoid single-letter primary key logic.'
    ),
    (
        'strcon',
        'STRC',
        'Strategy Stretch Preferred Tokenized Stock (Ondo)',
        '["STRC", "STRCon"]'::jsonb,
        'EVM',
        '["RWA", "tokenized_stock"]'::jsonb,
        'C',
        'mapping_risk',
        'low',
        'manual_review_required',
        'low',
        'watchlist',
        'Approximate tokenized stock mapping currently used in report and must be reviewed manually.',
        '["mapping_unclear", "low_liquidity"]'::jsonb,
        '2026-06-23 STRC、W、NEAR、EIGEN、ONDO、TAO 多币综合分析报告.md',
        1,
        'Do not allow strong automated conclusions until official mapping is confirmed.'
    ),
    (
        'bill',
        '$BILL',
        'Billions',
        '["$BILL", "BILL", "Billions"]'::jsonb,
        'TBD',
        '["AI", "DID", "identity", "KYA"]'::jsonb,
        'S',
        'core',
        'medium',
        'manual_review_required',
        'mid',
        'active',
        'AI agent identity and trust infrastructure thesis with strong research priority.',
        '["mapping_unclear", "unlock_risk", "commercialization_validation_pending"]'::jsonb,
        '2026-07-06 BILL综合分析报告.md',
        6,
        'High research priority, but external IDs and chain mapping should be completed before heavy automation.'
    )
ON CONFLICT (asset_id) DO UPDATE
SET
    symbol = EXCLUDED.symbol,
    project_name = EXCLUDED.project_name,
    aliases = EXCLUDED.aliases,
    chain_type = EXCLUDED.chain_type,
    sector_tags = EXCLUDED.sector_tags,
    research_priority = EXCLUDED.research_priority,
    research_bucket = EXCLUDED.research_bucket,
    mapping_confidence = EXCLUDED.mapping_confidence,
    mapping_review_status = EXCLUDED.mapping_review_status,
    liquidity_tier = EXCLUDED.liquidity_tier,
    status = EXCLUDED.status,
    base_case = EXCLUDED.base_case,
    risk_flags = EXCLUDED.risk_flags,
    first_seen_in_report = EXCLUDED.first_seen_in_report,
    report_coverage_count = EXCLUDED.report_coverage_count,
    notes = EXCLUDED.notes,
    updated_at = NOW();

COMMIT;
