INSERT INTO src_cg.coin_info (
    coin_id, symbol, name, description, homepage_url, image,
    genesis_date, market_cap_rank, coingecko_rank,
    categories, platforms, links,
    raw_response_id, fetched_at
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s::jsonb, %s::jsonb, %s::jsonb,
    %s, %s
)
ON CONFLICT (coin_id) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    homepage_url = EXCLUDED.homepage_url,
    image = EXCLUDED.image,
    genesis_date = EXCLUDED.genesis_date,
    market_cap_rank = EXCLUDED.market_cap_rank,
    coingecko_rank = EXCLUDED.coingecko_rank,
    categories = EXCLUDED.categories,
    platforms = EXCLUDED.platforms,
    links = EXCLUDED.links,
    raw_response_id = EXCLUDED.raw_response_id,
    fetched_at = EXCLUDED.fetched_at,
    updated_at = NOW()
