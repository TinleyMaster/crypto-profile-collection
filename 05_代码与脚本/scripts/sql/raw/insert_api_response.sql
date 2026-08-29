INSERT INTO raw.api_response (
    run_id,
    platform_code,
    endpoint_code,
    request_key,
    entity_key,
    page_key,
    payload,
    payload_hash,
    fetched_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s::jsonb,
    %s,
    %s::timestamptz
)
ON CONFLICT (platform_code, endpoint_code, COALESCE(request_key, ''), COALESCE(page_key, ''), payload_hash)
DO UPDATE SET
    run_id    = EXCLUDED.run_id,
    payload   = EXCLUDED.payload,
    fetched_at = EXCLUDED.fetched_at
RETURNING response_id;
