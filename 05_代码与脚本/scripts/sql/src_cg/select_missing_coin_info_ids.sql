SELECT COALESCE(
    ARRAY(
        SELECT l.coin_id
        FROM src_cg.coin_list l
        LEFT JOIN src_cg.coin_info i ON i.coin_id = l.coin_id
        WHERE i.coin_id IS NULL
        ORDER BY l.coin_id
        LIMIT %s
    ),
    ARRAY[]::text[]
) AS coin_ids;
