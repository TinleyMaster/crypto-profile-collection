UPDATE core.asset
SET
    canonical_symbol = %s,
    canonical_name = %s,
    asset_type = %s,
    status = %s,
    launch_date = COALESCE(%s::date, launch_date),
    description_short = COALESCE(%s, description_short),
    updated_at = NOW()
WHERE asset_id = %s
RETURNING asset_id;

