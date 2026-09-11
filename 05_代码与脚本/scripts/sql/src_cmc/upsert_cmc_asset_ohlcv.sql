INSERT INTO src_cmc.cmc_asset_ohlcv (
    cmc_id,
    time_open,
    time_period,
    open,
    high,
    low,
    close,
    volume,
    market_cap,
    raw_response_id
) VALUES (
    %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (cmc_id, time_open, time_period) DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    market_cap = EXCLUDED.market_cap,
    raw_response_id = EXCLUDED.raw_response_id;