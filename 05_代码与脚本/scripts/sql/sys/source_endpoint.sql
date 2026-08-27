-- sys.source_endpoint 表：数据源 API 端点元数据
-- 用于采集脚本注册、调用追踪、配额管理

CREATE TABLE IF NOT EXISTS sys.source_endpoint (
    endpoint_code       VARCHAR PRIMARY KEY,
    platform_code       VARCHAR NOT NULL,
    http_method         VARCHAR NOT NULL,
    endpoint_path       TEXT NOT NULL,
    entity_type         VARCHAR NOT NULL,
    update_granularity  VARCHAR,
    is_deprecated       BOOLEAN NOT NULL DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_endpoint_platform ON sys.source_endpoint(platform_code);

-- ============================================================
--  Seed 数据（所有已接入的 API 端点）
--  用 ON CONFLICT DO NOTHING，重复执行安全
-- ============================================================

INSERT INTO sys.source_endpoint (endpoint_code, platform_code, http_method, endpoint_path, entity_type, update_granularity, notes) VALUES
-- CoinGecko
('cg_coin',           'coingecko', 'GET', '/coins/{id}',                   'asset',          'batch_refresh', 'CoinGecko 币种详情'),
('cg_markets',        'coingecko', 'GET', '/coins/markets',                'asset_market',   'intraday',      'CoinGecko 市场快照'),

-- CoinGecko (旧命名空间 cg)
('coin_info',         'cg',        'GET', '/coins/{id}',                   'coin_detail',    'daily',         'Single coin detail with market data'),
('coin_list',         'cg',        'GET', '/coins/list',                   'coin_list',      'weekly',        'All supported coins list'),

-- CoinMarketCap
('cmc_map',           'cmc',       'GET', '/v1/cryptocurrency/map',        'asset',          'full_sync',     'CMC 全市场币种目录'),
('cmc_info',          'cmc',       'GET', '/v2/cryptocurrency/info',       'asset',          'batch_refresh', 'CMC 币种详情'),
('cmc_listings_latest','cmc',      'GET', '/v1/cryptocurrency/listings/latest', 'asset_market', 'intraday',   'CMC 全市场行情列表（按市值排名分页）'),
('cmc_quotes_latest', 'cmc',       'GET', '/v3/cryptocurrency/quotes/latest', 'asset_market', 'intraday',    'CMC 最新行情'),
('cmc_market_pairs',  'cmc',       'GET', '/v2/cryptocurrency/market-pairs/latest', 'asset_market', 'intraday', 'CMC 交易对快照'),
('cmc_categories',    'cmc',       'GET', '/v1/cryptocurrency/categories', 'category',       'daily',         'CMC 类别列表'),
('cmc_category',      'cmc',       'GET', '/v1/cryptocurrency/category',   'category_member','daily',         'CMC 类别成员'),

-- CMC DEX
('cmc_dex_token',         'cmc',   'GET', '/v1/dex/token',                 'dex_token',       'on_demand',    'CMC DEX Token 详情'),
('cmc_dex_token_pools',   'cmc',   'GET', '/v1/dex/token/pools',           'dex_pool',        'intraday',     'CMC DEX Token 池子'),
('cmc_dex_token_price',   'cmc',   'GET', '/v1/dex/token/price',           'dex_token_market','intraday',     'CMC DEX Token 价格'),
('cmc_dex_security',      'cmc',   'GET', '/v1/dex/security/detail',       'dex_security',    'intraday',     'CMC DEX Token 风险'),

-- DefiLlama
('llama_chains',     'defillama', 'GET', '/v2/chains',                     'chain',           'daily',         'DefiLlama 链列表'),
('llama_protocols',  'defillama', 'GET', '/protocols',                     'protocol',        'daily',         'DefiLlama 协议目录'),
('llama_protocol',   'defillama', 'GET', '/protocol/{slug}',               'protocol_metric', 'daily',         'DefiLlama 单协议详情'),

-- DeFiLlama (旧命名空间 dl)
('dl_protocols',     'dl',        'GET', '/protocols',                     'protocol_list',   'daily',         'All DeFi protocols with TVL')
ON CONFLICT (endpoint_code) DO NOTHING;
