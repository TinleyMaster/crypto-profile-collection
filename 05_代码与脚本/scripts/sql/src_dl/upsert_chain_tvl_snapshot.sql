-- Upsert chain TVL daily snapshot
INSERT INTO src_dl.chain_tvl_snapshot (
    chain_key, chain_name, snapshot_date, tvl_usd,
    tvl_change_1d, tvl_change_7d, tvl_change_30d,
    flow_1d_usd, flow_7d_usd, flow_30d_usd,
    raw_response_id, fetched_at
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s
)
ON CONFLICT (chain_key, snapshot_date) DO UPDATE SET
    chain_name = EXCLUDED.chain_name,
    tvl_usd = EXCLUDED.tvl_usd,
    tvl_change_1d = EXCLUDED.tvl_change_1d,
    tvl_change_7d = EXCLUDED.tvl_change_7d,
    tvl_change_30d = EXCLUDED.tvl_change_30d,
    flow_1d_usd = EXCLUDED.flow_1d_usd,
    flow_7d_usd = EXCLUDED.flow_7d_usd,
    flow_30d_usd = EXCLUDED.flow_30d_usd,
    raw_response_id = EXCLUDED.raw_response_id,
    updated_at = NOW()
