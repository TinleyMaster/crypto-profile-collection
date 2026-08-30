INSERT INTO src_cmc.cmc_asset_quote_snapshot (
    cmc_id,
    quote_time,
    price_usd,
    market_cap,
    fdv,
    volume_24h,
    circulating_supply,
    total_supply,
    max_supply,
    percent_change_1h,
    percent_change_24h,
    percent_change_7d,
    percent_change_30d,
    market_cap_dominance,
    raw_response_id,
    is_anomaly
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (cmc_id, quote_time) DO UPDATE SET
    price_usd = EXCLUDED.price_usd,
    market_cap = EXCLUDED.market_cap,
    fdv = EXCLUDED.fdv,
    volume_24h = EXCLUDED.volume_24h,
    circulating_supply = EXCLUDED.circulating_supply,
    total_supply = EXCLUDED.total_supply,
    max_supply = EXCLUDED.max_supply,
    percent_change_1h = EXCLUDED.percent_change_1h,
    percent_change_24h = EXCLUDED.percent_change_24h,
    percent_change_7d = EXCLUDED.percent_change_7d,
    percent_change_30d = EXCLUDED.percent_change_30d,
    market_cap_dominance = EXCLUDED.market_cap_dominance,
    raw_response_id = EXCLUDED.raw_response_id,
    is_anomaly = EXCLUDED.is_anomaly;
