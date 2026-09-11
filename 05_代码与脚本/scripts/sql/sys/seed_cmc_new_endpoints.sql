INSERT INTO sys.source_endpoint (endpoint_code, platform_code, http_method, endpoint_path, entity_type, update_granularity, is_deprecated, notes) VALUES
('cmc_macro_daily',      'cmc', 'GET', '/v1/global-metrics/quotes/latest', 'macro_global', 'daily', FALSE, 'CMC 宏观日频组合流程'),
('cmc_trending_daily',   'cmc', 'GET', '/v1/cryptocurrency/trending/latest', 'trending', 'intraday', FALSE, 'CMC 趋势榜组合流程'),
('cmc_airdrops',         'cmc', 'GET', '/v1/cryptocurrency/airdrops', 'airdrop', 'daily', FALSE, 'CMC 空投活动'),
('cmc_ohlcv_historical', 'cmc', 'GET', '/v2/cryptocurrency/ohlcv/historical', 'asset_market', 'intraday', FALSE, 'CMC 历史 OHLCV'),
('cmc_price_performance','cmc', 'GET', '/v2/cryptocurrency/price-performance-stats/latest', 'asset_market', 'intraday', FALSE, 'CMC 价格表现统计'),
('cmc_global_metrics',   'cmc', 'GET', '/v1/global-metrics/quotes/latest', 'macro_global', 'daily', FALSE, 'CMC 全球市场指标'),
('cmc_fear_greed',       'cmc', 'GET', '/v3/fear-and-greed', 'macro_sentiment', 'daily', FALSE, 'CMC 恐贪指数'),
('cmc_altcoin_season',   'cmc', 'GET', '/v1/altcoin-season-index', 'macro_season', 'daily', FALSE, 'CMC 山寨季指数'),
('cmc_trending_latest',  'cmc', 'GET', '/v1/cryptocurrency/trending/latest', 'trending', 'intraday', FALSE, 'CMC 搜索热度趋势榜'),
('cmc_trending_gainers', 'cmc', 'GET', '/v1/cryptocurrency/trending/gainers-losers', 'trending', 'intraday', FALSE, 'CMC 涨幅榜'),
('cmc_trending_losers',  'cmc', 'GET', '/v1/cryptocurrency/trending/gainers-losers', 'trending', 'intraday', FALSE, 'CMC 跌幅榜'),
('cmc_trending_most_visited', 'cmc', 'GET', '/v1/cryptocurrency/trending/most-visited', 'trending', 'intraday', FALSE, 'CMC 访问量趋势榜'),
('cmc_listings_new',     'cmc', 'GET', '/v1/cryptocurrency/listings/new', 'asset', 'intraday', FALSE, 'CMC 新上市币种'),
('cmc_dex_snapshot',     'cmc', 'GET', '/v1/dex/token', 'dex_token', 'intraday', FALSE, 'CMC DEX 组合快照流程')
ON CONFLICT (endpoint_code) DO NOTHING;