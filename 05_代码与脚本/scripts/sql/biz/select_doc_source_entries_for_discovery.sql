SELECT
    e.entry_id,
    e.entity_type,
    e.asset_id,
    e.protocol_id,
    e.source_code,
    e.entry_type,
    e.entry_url,
    e.discovered_from
FROM biz.doc_source_entry AS e
LEFT JOIN biz.doc_asset AS d
    ON d.entity_type = e.entity_type
   AND COALESCE(d.asset_id, -1) = COALESCE(e.asset_id, -1)
   AND COALESCE(d.protocol_id, -1) = COALESCE(e.protocol_id, -1)
   AND d.source_url = e.entry_url
WHERE d.doc_id IS NULL
ORDER BY
    CASE
        WHEN LOWER(e.entry_url) LIKE '%%.pdf%%'
          OR LOWER(e.entry_url) LIKE '%%whitepaper%%'
          OR LOWER(e.entry_url) LIKE '%%litepaper%%'
          OR LOWER(e.entry_url) LIKE '%%tokenomics%%'
          OR LOWER(e.entry_url) LIKE '%%audit%%'
          OR LOWER(e.entry_url) LIKE '%%deck%%'
          OR LOWER(e.entry_url) LIKE '%%paper%%'
          OR LOWER(e.entry_url) LIKE '%%docs/%%'
        THEN 1
        ELSE 2
    END,
    CASE e.entry_type
        WHEN 'docs' THEN 1
        WHEN 'official_website' THEN 2
        WHEN 'github' THEN 3
        WHEN 'medium' THEN 4
        ELSE 5
    END,
    e.entry_id
LIMIT %s;
