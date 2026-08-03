INSERT INTO src_dl.protocol_list (
    protocol_id, name, symbol, slug, category, chain, chains,
    tvl, change_1h, change_1d, change_7d,
    url, description, address, twitter,
    cmc_id, gecko_id,
    raw_response_id, fetched_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s::jsonb,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s,
    %s, %s
)
ON CONFLICT (protocol_id) DO UPDATE SET
    name = EXCLUDED.name,
    symbol = EXCLUDED.symbol,
    slug = EXCLUDED.slug,
    category = EXCLUDED.category,
    chain = EXCLUDED.chain,
    chains = EXCLUDED.chains,
    tvl = EXCLUDED.tvl,
    change_1h = EXCLUDED.change_1h,
    change_1d = EXCLUDED.change_1d,
    change_7d = EXCLUDED.change_7d,
    url = EXCLUDED.url,
    description = EXCLUDED.description,
    address = EXCLUDED.address,
    twitter = EXCLUDED.twitter,
    cmc_id = EXCLUDED.cmc_id,
    gecko_id = EXCLUDED.gecko_id,
    raw_response_id = EXCLUDED.raw_response_id,
    fetched_at = EXCLUDED.fetched_at,
    updated_at = NOW()
