-- 自有站点主题抢救目标清单（staging）。
--
-- 找出「有官网入口，但缺失自有站点主题」的资产，供
-- phase_b2_rescue_ownsite_topics.py 做 staging + 多轮深爬 + 按需 SPA 提升。
--
-- 「自有站点主题」= 能通过项目官网/文档站（含 sitemap.xml）在自有站点上
-- 直接发现的投研主题：国库/多签、团队/VC、审计、漏洞赏金、交易所上线、公告。
-- 这些主题不依赖第三方数据源，只要官网有对应页面，深爬就能救回。
--
-- 判定口径与 db_stats.py._compute_missing_materials 一致：
-- 汇总 biz.doc_source_entry / biz.doc_asset / biz.research_url 三表的 content_topics。
--
-- 排序：缺失主题多者优先 → 已采集文档入口多者优先（越是被跟踪的资产越先补）。
-- 过滤：剔除 symbol/name 不含字母数字的噪声资产（emoji/空白名）。
--
-- 使用方式（psql）：
--   psql "$DATABASE_URL" -f scripts/sql/biz/select_ownsite_rescue_targets.sql

WITH wanted AS (
    SELECT unnest(ARRAY[
        'treasury_multisig',   -- 国库 / 多签
        'team_vc',             -- 团队 / VC
        'audit',               -- 审计
        'bug_bounty',          -- 漏洞赏金
        'exchange_listing',    -- 交易所上线
        'announcement'         -- 公告
    ]::text[]) AS topic
),
topics AS (
    SELECT asset_id, topic FROM (
        SELECT asset_id, unnest(COALESCE(content_topics, '{}'::text[])) AS topic
        FROM biz.doc_source_entry WHERE entity_type = 'asset'
        UNION ALL
        SELECT asset_id, unnest(COALESCE(content_topics, '{}'::text[]))
        FROM biz.doc_asset
        UNION ALL
        SELECT asset_id, unnest(COALESCE(content_topics, '{}'::text[]))
        FROM biz.research_url
    ) t WHERE topic IS NOT NULL
),
present AS (
    SELECT asset_id, array_agg(DISTINCT topic) AS ts FROM topics GROUP BY asset_id
),
missing AS (
    SELECT a.asset_id,
           ARRAY(
               SELECT w.topic FROM wanted w
               WHERE NOT (w.topic = ANY(COALESCE(p.ts, '{}'::text[])))
           ) AS missing_topics
    FROM core.asset a
    LEFT JOIN present p USING (asset_id)
    WHERE EXISTS (
        -- 只救「有官网入口」的资产；没有官网就无从深爬自有站点
        SELECT 1 FROM biz.doc_source_entry w
        WHERE w.asset_id = a.asset_id AND w.entity_type = 'asset' AND w.entry_type = 'official_website'
    )
),
entry_counts AS (
    SELECT asset_id, count(*) AS entry_count
    FROM biz.doc_source_entry
    WHERE entity_type = 'asset'
    GROUP BY asset_id
)
SELECT a.asset_id,
       a.canonical_symbol,
       a.canonical_name,
       m.missing_topics,
       COALESCE(ec.entry_count, 0) AS entry_count
FROM missing m
JOIN core.asset a ON a.asset_id = m.asset_id
LEFT JOIN entry_counts ec ON ec.asset_id = m.asset_id
WHERE cardinality(m.missing_topics) > 0
  AND a.canonical_symbol ~ '[A-Za-z0-9]'
  AND a.canonical_name  ~ '[A-Za-z0-9]'
ORDER BY cardinality(m.missing_topics) DESC,
         COALESCE(ec.entry_count, 0) DESC,
         a.canonical_symbol ASC;
