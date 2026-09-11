INSERT INTO biz.global_metric_daily (
    metric_date,
    total_market_cap,
    total_volume_24h,
    btc_dominance,
    eth_dominance,
    stablecoin_market_cap,
    total_cryptocurrencies,
    active_cryptocurrencies,
    raw_ref,
    updated_at
) VALUES (
    %s::date, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW()
)
ON CONFLICT (metric_date) DO UPDATE SET
    total_market_cap = EXCLUDED.total_market_cap,
    total_volume_24h = EXCLUDED.total_volume_24h,
    btc_dominance = EXCLUDED.btc_dominance,
    eth_dominance = EXCLUDED.eth_dominance,
    stablecoin_market_cap = EXCLUDED.stablecoin_market_cap,
    total_cryptocurrencies = EXCLUDED.total_cryptocurrencies,
    active_cryptocurrencies = EXCLUDED.active_cryptocurrencies,
    raw_ref = EXCLUDED.raw_ref,
    updated_at = NOW();