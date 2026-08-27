SELECT
    asm.asset_id,
    p.protocol_id,
    p.url,
    p.twitter
FROM src_dl.protocol_list AS p
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'dl'
   AND asm.source_asset_key = p.protocol_id
WHERE
    ((p.url IS NOT NULL AND TRIM(p.url) != '') OR (p.twitter IS NOT NULL AND TRIM(p.twitter) != ''))
    -- 反连接改写：原 LEFT JOIN dse ... WHERE dse.entry_id IS NULL 会对
    -- doc_source_entry（1.8GB / 30 万行）走全表扫描，导致每日同步卡死。
    -- 改用 NOT EXISTS 走 (entity_type, asset_id) 前缀索引，实测 17x 提速。
    AND NOT EXISTS (
        SELECT 1
        FROM biz.doc_source_entry AS dse
        WHERE dse.entity_type = 'asset'
          AND dse.asset_id = asm.asset_id
          AND dse.source_code = 'dl'
    )
ORDER BY p.protocol_id
LIMIT %s;
