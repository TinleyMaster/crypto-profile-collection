INSERT INTO biz.catalyst_impact (
    catalyst_id, asset_id, impact_direction, impact_strength,
    horizon_days, derived_from
) VALUES (
    %s, %s, %s, %s, %s, %s
)
ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET
    impact_direction = EXCLUDED.impact_direction,
    impact_strength  = EXCLUDED.impact_strength,
    horizon_days     = EXCLUDED.horizon_days,
    derived_from     = EXCLUDED.derived_from
