-- 补建 core.asset_contract_map 视图
-- core.asset_contract 表已存在（由 phase_a_build_core.py 创建），
-- 但 phase_chain_transfer_monitor / phase_chain_onchain_query / phase_chain_holder_snapshot
-- 均引用 core.asset_contract_map，该视图缺失导致 SQL 执行失败。
-- 此视图作为 core.asset_contract 的别名，提供一致的查询接口。

CREATE OR REPLACE VIEW core.asset_contract_map AS
SELECT
    asset_id,
    chain,
    contract_address
FROM core.asset_contract;