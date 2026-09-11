INSERT INTO biz.airdrop_event (
    airdrop_id,
    project_name,
    description,
    status,
    coin_id,
    coin_symbol,
    coin_name,
    coin_slug,
    start_date,
    end_date,
    total_prize,
    winner_count,
    link,
    raw_ref,
    updated_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
    %s, %s, %s, %s::jsonb, NOW()
)
ON CONFLICT (airdrop_id) DO UPDATE SET
    project_name = EXCLUDED.project_name,
    description = EXCLUDED.description,
    status = EXCLUDED.status,
    coin_id = EXCLUDED.coin_id,
    coin_symbol = EXCLUDED.coin_symbol,
    coin_name = EXCLUDED.coin_name,
    coin_slug = EXCLUDED.coin_slug,
    start_date = EXCLUDED.start_date,
    end_date = EXCLUDED.end_date,
    total_prize = EXCLUDED.total_prize,
    winner_count = EXCLUDED.winner_count,
    link = EXCLUDED.link,
    raw_ref = EXCLUDED.raw_ref,
    updated_at = NOW();