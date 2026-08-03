SELECT
    m.cmc_id,
    m.symbol,
    m.name,
    m.slug,
    m.platform_name,
    m.token_address,
    i.description,
    i.date_launched,
    i.category_hint,
    i.urls,
    asm.asset_id AS existing_asset_id
FROM src_cmc.cmc_asset_map AS m
LEFT JOIN src_cmc.cmc_asset_info AS i
    ON i.cmc_id = m.cmc_id
LEFT JOIN core.asset_source_map AS asm
    ON asm.source_code = 'cmc'
   AND asm.source_asset_key = m.cmc_id::text
WHERE
    (%s::boolean IS TRUE OR asm.asset_id IS NULL)
ORDER BY m.cmc_id
LIMIT %s;

