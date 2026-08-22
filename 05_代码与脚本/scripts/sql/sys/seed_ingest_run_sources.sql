-- 修复 P1-3 / P1-5：登记链上持仓快照与衍生品采集的 ingest_run 来源。
-- phase_chain_holder_batch.py 使用 platform_code='onchain' / endpoint_code='holder_snapshot'，
-- phase_derivatives_batch.py 使用 platform_code='derivatives' / endpoint_code='batch_collect'，
-- 此前这两个值未在 sys.source_platform / sys.source_endpoint 登记，写入 sys.ingest_run 时
-- 触发外键约束失败，导致审计记录永远写不进、且污染主事务使整条管线崩溃。
-- 重复执行安全（ON CONFLICT DO NOTHING）。

INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description) VALUES
    ('onchain',     '链上数据(On-chain)',  NULL, '链上持仓/转账等链上原生数据，跨多链浏览器聚合'),
    ('derivatives', '衍生品数据(Derivatives)', NULL, '合约持仓/资金费率/OWI 等衍生品资金面数据')
ON CONFLICT (platform_code) DO NOTHING;

INSERT INTO sys.source_endpoint (endpoint_code, platform_code, http_method, endpoint_path, entity_type, update_granularity, is_deprecated, notes) VALUES
    ('holder_snapshot', 'onchain',     'GET', '/onchain/holder_snapshot', 'onchain_holder', 'daily',    FALSE, '链上 Top 持有者持仓快照（BSC/ETH/Base/Arb/Solana）'),
    ('batch_collect',   'derivatives', 'GET', '/derivatives/batch',       'asset_derivatives', 'intraday', FALSE, '衍生品资金面批量采集')
ON CONFLICT (endpoint_code) DO NOTHING;
