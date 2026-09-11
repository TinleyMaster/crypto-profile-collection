INSERT INTO src_cmc.cmc_dex_token_price_snapshot (
    platform_id,
    token_address,
    snapshot_time,
    chain_name,
    price_usd,
    market_cap,
    liquidity_usd,
    volume_24h,
    price_change_24h,
    raw_response_id
) VALUES (
    %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (platform_id, token_address, snapshot_time) DO UPDATE SET
    chain_name = EXCLUDED.chain_name,
    price_usd = EXCLUDED.price_usd,
    market_cap = EXCLUDED.market_cap,
    liquidity_usd = EXCLUDED.liquidity_usd,
    volume_24h = EXCLUDED.volume_24h,
    price_change_24h = EXCLUDED.price_change_24h,
    raw_response_id = EXCLUDED.raw_response_id;