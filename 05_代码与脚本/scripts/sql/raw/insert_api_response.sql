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
RETURNING response_id;

