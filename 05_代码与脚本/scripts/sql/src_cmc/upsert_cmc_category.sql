INSERT INTO src_cmc.cmc_category (
    category_id,
    category_name,
    title,
    description,
    num_tokens,
    market_cap,
    volume_24h,
    last_updated,
    raw_response_id,
    fetched_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s::timestamptz,
    %s,
    %s::timestamptz
)
ON CONFLICT (category_id) DO UPDATE SET
    category_name = EXCLUDED.category_name,
    title = EXCLUDED.title,
    description = EXCLUDED.description,
    num_tokens = EXCLUDED.num_tokens,
    market_cap = EXCLUDED.market_cap,
    volume_24h = EXCLUDED.volume_24h,
    last_updated = EXCLUDED.last_updated,
    raw_response_id = EXCLUDED.raw_response_id,
    fetched_at = EXCLUDED.fetched_at,
    updated_at = NOW();
