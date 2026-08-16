-- 目标资产清单：新币（date_launched >= 2025-01-01）∪ 热门赛道（全部 7 类）。
--
-- 用于节流批量补齐单个代币的投研资料（配合 scripts/bin/collect_assets_batch.py）。
-- 赛道信号来自 src_cmc.cmc_asset_info.tags（CMC 标签），
-- 因为 src_cmc.cmc_category / src_cg.cg_coin_detail 当前为空表。
--
-- 输出列：asset_id, canonical_symbol, canonical_name, date_launched,
--         is_new, sectors, entry_count
-- 排序：新币优先（launch_date 降序）→ 已采集文档数升序（越少越先补）。

WITH cmc AS (
    -- 一个资产可能映射多个 cmc_id，取 date_launched 最新的那条作为代表
    SELECT DISTINCT ON (m.asset_id)
           m.asset_id,
           i.cmc_id,
           i.date_launched,
           i.tags,
           i.category_hint
    FROM core.asset_source_map m
    JOIN src_cmc.cmc_asset_info i ON i.cmc_id = m.source_asset_key::bigint
    WHERE m.source_code = 'cmc'
      AND NOT (i.tags ?| ARRAY['tokenized-stock','tradfi-assets-derivatives','synthetics','rehypothecated-crypto'])
    ORDER BY m.asset_id, i.date_launched DESC NULLS LAST
),
target AS (
    SELECT
        asset_id,
        date_launched,
        category_hint,
        ARRAY_REMOVE(ARRAY[
            CASE WHEN tags ?| ARRAY['ai-big-data','ai-agents'] THEN 'ai' END,
            CASE WHEN tags ?| ARRAY['memes','animal-memes','pump-fun-ecosystem'] THEN 'meme' END,
            CASE WHEN tags ?| ARRAY['defi'] THEN 'defi' END,
            CASE WHEN tags ?| ARRAY['real-world-assets-protocols'] THEN 'rwa' END,
            CASE WHEN tags ?| ARRAY['depin'] THEN 'depin' END,
            CASE WHEN tags ?| ARRAY['layer-1','layer-2'] THEN 'l1l2' END,
            CASE WHEN tags ?| ARRAY['gaming','play-to-earn'] THEN 'gamefi' END
        ], NULL) AS sectors
    FROM cmc
),
entry_counts AS (
    SELECT asset_id, count(*) AS entry_count
    FROM biz.doc_source_entry
    GROUP BY asset_id
)
SELECT
    a.asset_id,
    a.canonical_symbol,
    a.canonical_name,
    t.date_launched,
    (t.date_launched >= '2025-01-01') AS is_new,
    t.sectors,
    COALESCE(ec.entry_count, 0) AS entry_count
FROM target t
JOIN core.asset a ON a.asset_id = t.asset_id
LEFT JOIN entry_counts ec ON ec.asset_id = a.asset_id
WHERE (t.date_launched >= '2025-01-01' OR cardinality(t.sectors) > 0)
  AND a.canonical_name NOT LIKE '%Derivatives%'
  AND a.canonical_name NOT LIKE '%Bridged%'
ORDER BY COALESCE((t.date_launched >= '2025-01-01'), false) DESC,
         t.date_launched DESC NULLS LAST,
         entry_count ASC,
         a.canonical_symbol ASC;
