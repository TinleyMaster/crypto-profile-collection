-- ============================================================
-- 清理 biz.asset_tokenomics.raw_text 里的"模型/平台 JSON 残渣"
--
-- 背景：
--   raw_text 字段设计意图是"原始提取文本（用于校验）"，但实际入库的是
--   LLM 抽取的 JSON 输出（extracted_by='llm'）或 tokenomics.com 平台 JSON
--   （extracted_by='tokenomist'）——整段都是 JSON，并非源文档正文。
--   该 JSON 里嵌着 supply 数值（如 ETH 的 notes："Total supply is from
--   CoinGecko API and is in millions (244.0862694 million ETH)"），会被下游
--   研究结论 LLM 当作权威来源读走，污染结论。
--   "A 清理"(cleanup_polluted_tokenomics.sql) 只 DELETE 整条污染记录 / 结构化
--   字段被 validate_supply_units 覆盖，但 raw_text 残渣从未被碰。
--
-- 目标：把"被判定污染的 tokenomics 记录"的 raw_text 置空（保留结构化字段），
--       避免误导结论 LLM。仅清理残渣，不删记录、不动结构化字段。
--
-- 命中范围（预览已核对）：
--   (a) 低置信 + A 清理污染域名/不匹配备注
--   (b) raw_text 里提取的 total_supply 与 CMC 权威快照偏离 >10× 的残渣
--   (c) raw_text 含单位误导 supply 备注（million/billion/亿/trillion）
-- ============================================================

-- 先预览将要清理的记录
SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
       t.confidence, t.extracted_by,
       LENGTH(t.raw_text) AS raw_len,
       CASE
         WHEN t.confidence < 0.6
              AND (EXISTS (SELECT 1 FROM unnest(t.source_urls) u
                           WHERE u ILIKE '%alpha.wtf%' OR u ILIKE '%batcat.lol%'
                              OR u ILIKE '%feg.io%' OR u ILIKE '%bitgertswap%'
                              OR u ILIKE '%flap.sh%' OR u ILIKE '%ape.pro%'
                              OR u ILIKE '%hpop8i%' OR u ILIKE '%dashdapp.io%'
                              OR u ILIKE '%stargate.finance%' OR u ILIKE '%wormholenetwork%'
                              OR u ILIKE '%bridge.linea.build%')
                OR t.extraction_notes ILIKE '%并非%官方%'
                OR t.extraction_notes ILIKE '%可能匹配的是%'
                OR t.extraction_notes ILIKE '%大量无关%')
           THEN 'A清理污染'
         WHEN t.raw_text IS NOT NULL
              AND (t.raw_text::jsonb->>'total_supply') IS NOT NULL
              AND (t.raw_text::jsonb->>'total_supply') ~ '^[0-9.]+$'
              AND EXISTS (
                WITH latest AS (
                  SELECT DISTINCT ON (cmc_id) cmc_id, total_supply AS cmc_ts
                  FROM src_cmc.cmc_asset_quote_snapshot ORDER BY cmc_id, quote_time DESC
                )
                SELECT 1 FROM core.asset_source_map m
                JOIN latest l ON l.cmc_id = m.source_asset_key::bigint
                WHERE m.asset_id = t.asset_id AND m.source_code='cmc' AND m.is_primary=TRUE
                  AND l.cmc_ts IS NOT NULL AND l.cmc_ts > 0
                  AND (GREATEST((t.raw_text::jsonb->>'total_supply')::numeric, l.cmc_ts)
                       / LEAST((t.raw_text::jsonb->>'total_supply')::numeric, l.cmc_ts)) > 10
              )
           THEN 'raw与CMC偏离>10x'
         WHEN t.raw_text ~* '"(notes|note)"[^}]*(million|billion|trillion|亿|万亿)'
           THEN '单位误导备注'
         ELSE '其他'
       END AS hit_reason
FROM biz.asset_tokenomics t
JOIN core.asset a ON a.asset_id = t.asset_id
WHERE t.raw_text IS NOT NULL
  AND t.raw_text LIKE '{%'
  AND (
    -- (a) A 清理污染判定
    (t.confidence < 0.6
       AND (EXISTS (SELECT 1 FROM unnest(t.source_urls) u
                    WHERE u ILIKE '%alpha.wtf%' OR u ILIKE '%batcat.lol%'
                       OR u ILIKE '%feg.io%' OR u ILIKE '%bitgertswap%'
                       OR u ILIKE '%flap.sh%' OR u ILIKE '%ape.pro%'
                       OR u ILIKE '%hpop8i%' OR u ILIKE '%dashdapp.io%'
                       OR u ILIKE '%stargate.finance%' OR u ILIKE '%wormholenetwork%'
                       OR u ILIKE '%bridge.linea.build%')
             OR t.extraction_notes ILIKE '%并非%官方%'
             OR t.extraction_notes ILIKE '%可能匹配的是%'
             OR t.extraction_notes ILIKE '%大量无关%'))
    -- (b) raw_text 提取的 total_supply 与 CMC 快照偏离 >10×
    OR (t.raw_text::jsonb->>'total_supply') IS NOT NULL
       AND (t.raw_text::jsonb->>'total_supply') ~ '^[0-9.]+$'
       AND EXISTS (
         WITH latest AS (
           SELECT DISTINCT ON (cmc_id) cmc_id, total_supply AS cmc_ts
           FROM src_cmc.cmc_asset_quote_snapshot ORDER BY cmc_id, quote_time DESC
         )
         SELECT 1 FROM core.asset_source_map m
         JOIN latest l ON l.cmc_id = m.source_asset_key::bigint
         WHERE m.asset_id = t.asset_id AND m.source_code='cmc' AND m.is_primary=TRUE
           AND l.cmc_ts IS NOT NULL AND l.cmc_ts > 0
           AND (GREATEST((t.raw_text::jsonb->>'total_supply')::numeric, l.cmc_ts)
                / LEAST((t.raw_text::jsonb->>'total_supply')::numeric, l.cmc_ts)) > 10
       )
    -- (c) 单位误导 supply 备注
    OR t.raw_text ~* '"(notes|note)"[^}]*(million|billion|trillion|亿|万亿)'
  )
ORDER BY hit_reason, a.asset_id;

-- 执行清理（确认上面的结果后再打开）：把残渣 raw_text 置空，保留结构化字段
-- UPDATE biz.asset_tokenomics t
-- SET raw_text = NULL, updated_at = NOW()
-- FROM core.asset a
-- WHERE t.asset_id = a.asset_id
--   AND t.raw_text IS NOT NULL
--   AND t.raw_text LIKE '{%'
--   AND ( ... 同上 WHERE 条件 ... );
