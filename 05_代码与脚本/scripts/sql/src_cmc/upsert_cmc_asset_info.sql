INSERT INTO src_cmc.cmc_asset_info (
    cmc_id,
    description,
    logo,
    notice,
    date_launched,
    tags,
    urls,
    platform_json,
    category_hint,
    raw_response_id,
    fetched_at,
    updated_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s::date,
    %s::jsonb,
    %s::jsonb,
    %s::jsonb,
    %s,
    %s,
    %s::timestamptz,
    NOW()
)
ON CONFLICT (cmc_id) DO UPDATE SET
    description = EXCLUDED.description,
    logo = EXCLUDED.logo,
    notice = EXCLUDED.notice,
    date_launched = COALESCE(EXCLUDED.date_launched, src_cmc.cmc_asset_info.date_launched),
    tags = EXCLUDED.tags,
    urls = EXCLUDED.urls,
    platform_json = EXCLUDED.platform_json,
    category_hint = EXCLUDED.category_hint,
    raw_response_id = EXCLUDED.raw_response_id,
    fetched_at = EXCLUDED.fetched_at,
    updated_at = NOW();

