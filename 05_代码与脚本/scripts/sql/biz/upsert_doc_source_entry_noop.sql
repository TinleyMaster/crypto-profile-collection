INSERT INTO biz.doc_source_entry (
    entity_type,
    asset_id,
    protocol_id,
    source_code,
    entry_type,
    entry_url,
    discovered_from,
    is_primary,
    content_topics,
    classify_method,
    classify_confidence,
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
    NOW()
)
ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url) DO NOTHING
