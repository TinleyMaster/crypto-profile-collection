INSERT INTO biz.doc_asset (
    entity_type,
    asset_id,
    protocol_id,
    entry_id,
    doc_type,
    source_url,
    resolved_url,
    file_name,
    mime_type,
    file_size_bytes,
    parse_status,
    sync_status,
    last_seen_at,
    updated_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    NOW(),
    NOW()
)
ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), source_url) DO UPDATE SET
    entry_id = EXCLUDED.entry_id,
    doc_type = EXCLUDED.doc_type,
    resolved_url = EXCLUDED.resolved_url,
    file_name = EXCLUDED.file_name,
    mime_type = EXCLUDED.mime_type,
    file_size_bytes = EXCLUDED.file_size_bytes,
    last_seen_at = NOW(),
    updated_at = NOW()
RETURNING doc_id;

