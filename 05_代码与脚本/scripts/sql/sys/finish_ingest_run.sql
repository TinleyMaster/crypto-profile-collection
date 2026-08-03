UPDATE sys.ingest_run
SET
    status = %s,
    http_status = %s,
    total_items = %s,
    success_items = %s,
    fail_items = %s,
    finished_at = NOW(),
    duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000,
    error_message = %s
WHERE run_id = %s
RETURNING run_id, status;

