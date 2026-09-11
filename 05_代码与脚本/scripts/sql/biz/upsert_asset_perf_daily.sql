INSERT INTO biz.asset_perf_daily (
    asset_id,
    perf_date,
    source_code,
    ath_price,
    ath_timestamp,
    atl_price,
    atl_timestamp,
    drawdown_from_ath,
    raw_ref,
    updated_at
) VALUES (
    %s, %s::date, %s, %s, %s::timestamptz, %s, %s::timestamptz, %s, %s::jsonb, NOW()
)
ON CONFLICT (asset_id, perf_date, source_code) DO UPDATE SET
    ath_price = EXCLUDED.ath_price,
    ath_timestamp = EXCLUDED.ath_timestamp,
    atl_price = EXCLUDED.atl_price,
    atl_timestamp = EXCLUDED.atl_timestamp,
    drawdown_from_ath = EXCLUDED.drawdown_from_ath,
    raw_ref = EXCLUDED.raw_ref,
    updated_at = NOW();