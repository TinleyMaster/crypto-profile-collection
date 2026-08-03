INSERT INTO sys.ingest_run (
    platform_code,
    endpoint_code,
    workflow_name,
    request_params,
    request_url,
    status,
    started_at
) VALUES (
    %s,
    %s,
    %s,
    %s::jsonb,
    %s,
    'running',
    NOW()
)
RETURNING run_id, started_at;

