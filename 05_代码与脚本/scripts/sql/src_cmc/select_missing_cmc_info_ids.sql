SELECT COALESCE(
    ARRAY(
        SELECT m.cmc_id
        FROM src_cmc.cmc_asset_map AS m
        LEFT JOIN src_cmc.cmc_asset_info AS i
            ON i.cmc_id = m.cmc_id
        WHERE i.cmc_id IS NULL
        ORDER BY m.cmc_id
        LIMIT %s
    ),
    ARRAY[]::bigint[]
) AS cmc_ids;

