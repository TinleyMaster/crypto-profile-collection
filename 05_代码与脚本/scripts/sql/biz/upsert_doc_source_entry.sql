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
ON CONFLICT (entity_type, COALESCE(asset_id, -1), COALESCE(protocol_id, -1), entry_url) DO UPDATE SET
    source_code = EXCLUDED.source_code,
    entry_type = EXCLUDED.entry_type,
    discovered_from = EXCLUDED.discovered_from,
    is_primary = EXCLUDED.is_primary,
    content_topics = EXCLUDED.content_topics,
    classify_method = EXCLUDED.classify_method,
    classify_confidence = EXCLUDED.classify_confidence,
    updated_at = NOW()
RETURNING entry_id;

