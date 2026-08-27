SELECT
    asm.asset_id,
    i.coin_id,
    i.homepage_url,
    i.links
FROM src_cg.coin_info AS i
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'cg'
   AND asm.source_asset_key = i.coin_id
WHERE
    (i.homepage_url IS NOT NULL OR i.links IS NOT NULL)
    -- 反连接改写：原 LEFT JOIN dse ... WHERE dse.entry_id IS NULL 会对
    -- doc_source_entry（1.8GB / 30 万行）走全表扫描，导致每日同步卡死。
    -- 改用 NOT EXISTS 走 (entity_type, asset_id) 前缀索引，实测 17x 提速。
    AND NOT EXISTS (
        SELECT 1
        FROM biz.doc_source_entry AS dse
        WHERE dse.entity_type = 'asset'
          AND dse.asset_id = asm.asset_id
          AND dse.source_code = 'cg'
    )
ORDER BY i.coin_id
LIMIT %s;
