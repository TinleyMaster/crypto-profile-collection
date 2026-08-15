SELECT
    asm.asset_id,
    i.cmc_id,
    i.urls
FROM src_cmc.cmc_asset_info AS i
INNER JOIN core.asset_source_map AS asm
    ON asm.source_code = 'cmc'
   AND asm.source_asset_key = i.cmc_id::text
WHERE
    i.urls IS NOT NULL
    -- 增量 + 补缺：只要 CMC 里还有「未入库」的 URL，就重新提取该资产。
    -- 这样能覆盖两类情况：
    --   1. 新映射资产（完全没有 doc_source_entry）
    --   2. 已映射资产但某个 url_key（如 website/technical_doc）此前缺失，
    --      或被 B2 覆盖 provenance 后又被重爬误删
    AND EXISTS (
        SELECT 1
        FROM jsonb_each(i.urls) AS kv
        WHERE jsonb_typeof(kv.value) = 'array'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(kv.value) AS u
              WHERE btrim(u) <> ''
                AND NOT EXISTS (
                    SELECT 1
                    FROM biz.doc_source_entry AS dse
                    WHERE dse.entity_type = 'asset'
                      AND dse.asset_id = asm.asset_id
                      AND dse.entry_url = btrim(u)
                )
          )
    )
ORDER BY i.cmc_id
LIMIT %s;
