-- 登记 DeFiLlama 链 TVL 采集的 ingest_run 来源。
-- ingest_llama_chain_tvl.py 使用 platform_code='defillama' / endpoint_code='llama_chains' / 'llama_chain_hist_tvl'。
-- 重复执行安全（ON CONFLICT DO NOTHING）。

INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description) VALUES
    ('defillama', 'DeFiLlama', 'https://api.llama.fi', 'DeFiLlama 免费 TVL/协议/链数据 API')
ON CONFLICT (platform_code) DO NOTHING;

INSERT INTO sys.source_endpoint (endpoint_code, platform_code, http_method, endpoint_path, entity_type, update_granularity, is_deprecated, notes) VALUES
    ('llama_chains',          'defillama', 'GET', '/v2/chains',                    'chain_tvl',        'daily', FALSE, '全量链最新 TVL 列表（一次请求）'),
    ('llama_chain_hist_tvl',  'defillama', 'GET', '/v2/historicalChainTvl/{chain}', 'chain_tvl_snapshot', 'daily', FALSE, '单链历史 TVL（每条链一次请求，用于计算净流入）')
ON CONFLICT (endpoint_code) DO NOTHING;
