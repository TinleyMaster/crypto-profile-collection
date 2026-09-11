INSERT INTO biz.asset_trending (
    snapshot_date,
    trend_type,
    time_period,
    cmc_id,
    symbol,
    name,
    slug,
    rank_num,
    price_usd,
    market_cap,
    volume_24h,
    percent_change_24h,
    raw_ref,
    updated_at
) VALUES (
    %s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW()
)
ON CONFLICT (snapshot_date, trend_type, time_period, cmc_id) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    slug = EXCLUDED.slug,
    rank_num = EXCLUDED.rank_num,
    price_usd = EXCLUDED.price_usd,
    market_cap = EXCLUDED.market_cap,
    volume_24h = EXCLUDED.volume_24h,
    percent_change_24h = EXCLUDED.percent_change_24h,
    raw_ref = EXCLUDED.raw_ref,
    updated_at = NOW();