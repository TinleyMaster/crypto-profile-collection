INSERT INTO core.asset (
    canonical_symbol,
    canonical_name,
    asset_type,
    status,
    launch_date,
    description_short
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s::date,
    %s
)
RETURNING asset_id;

