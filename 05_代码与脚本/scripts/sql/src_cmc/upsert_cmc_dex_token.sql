INSERT INTO src_cmc.cmc_dex_token (
    platform_id,
    chain_name,
    token_address,
    symbol,
    name,
    decimals,
    project_url,
    logo,
    raw_response_id,
    updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
)
ON CONFLICT (platform_id, token_address) DO UPDATE SET
    chain_name = EXCLUDED.chain_name,
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    decimals = EXCLUDED.decimals,
    project_url = EXCLUDED.project_url,
    logo = EXCLUDED.logo,
    raw_response_id = EXCLUDED.raw_response_id,
    updated_at = NOW();