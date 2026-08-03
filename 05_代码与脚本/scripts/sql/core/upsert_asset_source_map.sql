INSERT INTO core.asset_source_map (
    asset_id,
    source_code,
    source_asset_key,
    match_status,
    match_method,
    match_confidence,
    is_primary,
    verified_by,
    verified_at,
    updated_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    NOW(),
    NOW()
)
ON CONFLICT (source_code, source_asset_key) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    match_status = EXCLUDED.match_status,
    match_method = EXCLUDED.match_method,
    match_confidence = EXCLUDED.match_confidence,
    is_primary = EXCLUDED.is_primary,
    verified_by = EXCLUDED.verified_by,
    verified_at = NOW(),
    updated_at = NOW()
RETURNING asset_id;
