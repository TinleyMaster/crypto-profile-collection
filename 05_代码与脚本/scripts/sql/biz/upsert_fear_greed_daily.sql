INSERT INTO biz.fear_greed_daily (
    metric_date,
    value,
    value_classification,
    value_class,
    raw_ref,
    updated_at
) VALUES (
    %s::date, %s, %s, %s, %s::jsonb, NOW()
)
ON CONFLICT (metric_date) DO UPDATE SET
    value = EXCLUDED.value,
    value_classification = EXCLUDED.value_classification,
    value_class = EXCLUDED.value_class,
    raw_ref = EXCLUDED.raw_ref,
    updated_at = NOW();