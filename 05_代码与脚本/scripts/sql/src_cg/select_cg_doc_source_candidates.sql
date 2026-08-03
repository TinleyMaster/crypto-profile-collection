SELECT
    asm.asset_id,
    i.coin_id,
    i.homepage_url,
    i.links
FROM src_cg.coin_info AS i
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'cg'
   AND asm.source_asset_key = i.coin_id
LEFT JOIN biz.doc_source_entry AS dse
    ON dse.entity_type = 'asset'
   AND dse.asset_id = asm.asset_id
   AND dse.source_code = 'cg'
WHERE
    (i.homepage_url IS NOT NULL OR i.links IS NOT NULL)
    AND dse.entry_id IS NULL
ORDER BY i.coin_id
LIMIT %s;
