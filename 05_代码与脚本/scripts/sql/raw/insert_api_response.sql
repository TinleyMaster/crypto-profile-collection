SELECT raw.upsert_api_response(
    %s,   -- run_id
    %s,   -- platform_code
    %s,   -- endpoint_code
    %s,   -- request_key
    %s,   -- entity_key
    %s,   -- page_key
    %s::jsonb,  -- payload
    %s,   -- payload_hash
    %s::timestamptz  -- fetched_at
) AS response_id;
