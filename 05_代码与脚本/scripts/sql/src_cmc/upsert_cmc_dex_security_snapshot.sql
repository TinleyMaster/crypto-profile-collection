INSERT INTO src_cmc.cmc_dex_security_snapshot (
    platform_id,
    token_address,
    snapshot_time,
    chain_name,
    is_honeypot,
    buy_tax,
    sell_tax,
    can_take_back_ownership,
    owner_address,
    risk_level,
    security_flags,
    raw_response_id
) VALUES (
    %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
)
ON CONFLICT (platform_id, token_address, snapshot_time) DO UPDATE SET
    chain_name = EXCLUDED.chain_name,
    is_honeypot = EXCLUDED.is_honeypot,
    buy_tax = EXCLUDED.buy_tax,
    sell_tax = EXCLUDED.sell_tax,
    can_take_back_ownership = EXCLUDED.can_take_back_ownership,
    owner_address = EXCLUDED.owner_address,
    risk_level = EXCLUDED.risk_level,
    security_flags = EXCLUDED.security_flags,
    raw_response_id = EXCLUDED.raw_response_id;