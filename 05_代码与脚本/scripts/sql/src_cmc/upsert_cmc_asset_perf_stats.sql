INSERT INTO src_cmc.cmc_asset_perf_stats (
    cmc_id,
    snapshot_time,
    time_period,
    open,
    high,
    low,
    close,
    percent_change,
    price_change,
    open_timestamp,
    high_timestamp,
    low_timestamp,
    close_timestamp,
    raw_response_id
) VALUES (
    %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s,
    %s::timestamptz, %s::timestamptz, %s::timestamptz, %s::timestamptz, %s
)
ON CONFLICT (cmc_id, snapshot_time, time_period) DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    percent_change = EXCLUDED.percent_change,
    price_change = EXCLUDED.price_change,
    open_timestamp = EXCLUDED.open_timestamp,
    high_timestamp = EXCLUDED.high_timestamp,
    low_timestamp = EXCLUDED.low_timestamp,
    close_timestamp = EXCLUDED.close_timestamp,
    raw_response_id = EXCLUDED.raw_response_id;