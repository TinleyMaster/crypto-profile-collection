SELECT COALESCE(
    ARRAY(
        SELECT l.coin_id
        FROM src_cg.coin_list l
        LEFT JOIN src_cg.coin_info i ON i.coin_id = l.coin_id
        -- 已入库 core.asset 的优先（需要补文档/合约地址）
        LEFT JOIN core.asset_source_map asm
            ON asm.source_code = 'cg' AND asm.source_asset_key = l.coin_id
        -- 有 CMC 排名的次之（有真实交易/市值，投研价值更高）
        LEFT JOIN src_cmc.cmc_asset_map cm
            ON UPPER(cm.symbol) = UPPER(l.symbol) AND cm.token_address IS NOT NULL
        WHERE i.coin_id IS NULL
        GROUP BY l.coin_id
        ORDER BY
            -- ① 已入库资产优先
            (COUNT(asm.asset_id) = 0) ASC,
            -- ② 有 CMC 排名优先
            (MIN(cm.rank_num) IS NULL) ASC,
            -- ③ 按 CMC rank 升序（rank 越小越重要）
            MIN(cm.rank_num) ASC,
            -- ④ 其余按 coin_id 兜底
            l.coin_id
        LIMIT %s
    ),
    ARRAY[]::text[]
) AS coin_ids;
