SELECT
    asm.asset_id,
    p.protocol_id,
    p.url,
    p.twitter
FROM src_dl.protocol_list AS p
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'dl'
   AND asm.source_asset_key = p.protocol_id
LEFT JOIN biz.doc_source_entry AS dse
    ON dse.entity_type = 'asset'
   AND dse.asset_id = asm.asset_id
   AND dse.source_code = 'dl'
WHERE
    (p.url IS NOT NULL OR p.twitter IS NOT NULL)
    AND dse.entry_id IS NULL
ORDER BY p.protocol_id
LIMIT %s;
