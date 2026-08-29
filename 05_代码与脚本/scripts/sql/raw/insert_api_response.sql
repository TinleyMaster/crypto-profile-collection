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
ON CONFLICT ON INDEX uq_raw_api_response_dedup
DO UPDATE SET
    run_id    = EXCLUDED.run_id,
    payload   = EXCLUDED.payload,
    fetched_at = EXCLUDED.fetched_at
RETURNING response_id;

