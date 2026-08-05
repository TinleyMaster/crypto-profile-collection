SELECT
    asm.asset_id,
    i.cmc_id,
    i.urls
FROM src_cmc.cmc_asset_info AS i
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'cmc'
   AND asm.source_asset_key = i.cmc_id::text
LEFT JOIN biz.doc_source_entry AS dse
    ON dse.entity_type = 'asset'
   AND dse.asset_id = asm.asset_id
   AND dse.source_code = 'cmc'
WHERE
    i.urls IS NOT NULL
ORDER BY i.cmc_id
LIMIT %s;
