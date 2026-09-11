INSERT INTO src_cmc.cmc_dex_pool_snapshot (
    platform_id,
    token_address,
    snapshot_time,
    pool_address,
    dex_name,
    pair_name,
    liquidity_usd,
    volume_24h,
    fee_rate,
    chain_name,
    raw_response_id
) VALUES (
    %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (platform_id, token_address, snapshot_time, pool_address) DO UPDATE SET
    dex_name = EXCLUDED.dex_name,
    pair_name = EXCLUDED.pair_name,
    liquidity_usd = EXCLUDED.liquidity_usd,
    volume_24h = EXCLUDED.volume_24h,
    fee_rate = EXCLUDED.fee_rate,
    chain_name = EXCLUDED.chain_name,
    raw_response_id = EXCLUDED.raw_response_id;