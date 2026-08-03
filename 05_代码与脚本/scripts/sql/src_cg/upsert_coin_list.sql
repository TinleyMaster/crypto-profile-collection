INSERT INTO src_cg.coin_list (
    coin_id, symbol, name, platforms
) VALUES (
    %s, %s, %s, %s::jsonb
)
ON CONFLICT (coin_id) DO UPDATE SET
    symbol = EXCLUDED.symbol,
    name = EXCLUDED.name,
    platforms = EXCLUDED.platforms,
    updated_at = NOW()
