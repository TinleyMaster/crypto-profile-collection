INSERT INTO biz.altcoin_season_daily (
    metric_date,
    value,
    raw_ref,
    updated_at
) VALUES (
    %s::date, %s, %s::jsonb, NOW()
)
ON CONFLICT (metric_date) DO UPDATE SET
    value = EXCLUDED.value,
    raw_ref = EXCLUDED.raw_ref,
    updated_at = NOW();