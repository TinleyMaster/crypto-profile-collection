INSERT INTO src_cmc.cmc_category_member (
    category_id,
    cmc_id,
    snapshot_date,
    rank_in_category,
    market_cap,
    percent_change_24h,
    raw_response_id,
    created_at
) VALUES (
    %s,
    %s,
    %s::date,
    %s,
    %s,
    %s,
    %s,
    NOW()
)
ON CONFLICT (category_id, cmc_id, snapshot_date) DO UPDATE SET
    rank_in_category = EXCLUDED.rank_in_category,
    market_cap = EXCLUDED.market_cap,
    percent_change_24h = EXCLUDED.percent_change_24h,
    raw_response_id = EXCLUDED.raw_response_id;
