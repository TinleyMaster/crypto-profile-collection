INSERT INTO src_cmc.cmc_market_pair_snapshot (
    cmc_id,
    snapshot_time,
    exchange_name,
    market_pair,
    market_type,
    category,
    pair_base_symbol,
    pair_quote_symbol,
    price,
    volume_24h,
    liquidity_usd,
    market_url,
    outlier_score,
    effective_liquidity,
    raw_response_id
) VALUES (
    %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (cmc_id, snapshot_time, market_pair) DO UPDATE SET
    exchange_name = EXCLUDED.exchange_name,
    market_type = EXCLUDED.market_type,
    category = EXCLUDED.category,
    pair_base_symbol = EXCLUDED.pair_base_symbol,
    pair_quote_symbol = EXCLUDED.pair_quote_symbol,
    price = EXCLUDED.price,
    volume_24h = EXCLUDED.volume_24h,
    liquidity_usd = EXCLUDED.liquidity_usd,
    market_url = EXCLUDED.market_url,
    outlier_score = EXCLUDED.outlier_score,
    effective_liquidity = EXCLUDED.effective_liquidity,
    raw_response_id = EXCLUDED.raw_response_id;