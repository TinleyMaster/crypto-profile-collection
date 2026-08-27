-- 查找已映射 DefiLlama、但尚未补齐第三方(审计/评级)链接的资产。
-- 以 doc_source_entry.discovered_from='dl_protocol.rating' 条目作为「已处理」标记（无论是否存在审计链接都会写入）。
SELECT
    asm.asset_id,
    p.protocol_id,
    p.slug,
    p.name
FROM src_dl.protocol_list AS p
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'dl'
   AND asm.source_asset_key = p.protocol_id
WHERE
    p.slug IS NOT NULL
    AND TRIM(p.slug) != ''
    AND NOT EXISTS (
        SELECT 1
        FROM biz.doc_source_entry AS dse
        WHERE dse.entity_type = 'asset'
          AND dse.asset_id = asm.asset_id
          AND dse.source_code = 'dl'
          AND dse.discovered_from = 'dl_protocol.rating'
    )
ORDER BY asm.asset_id
LIMIT %s;
