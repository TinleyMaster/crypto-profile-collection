-- 缺失投研资料类型分布诊断。
--
-- 复刻 workbench/db_stats.py 的 _compute_missing_materials 判定逻辑，
-- 输出每类投研资料「缺失了多少资产」，按缺失数降序，用于决定先补齐哪类资料。
--
-- 使用方式（psql）：
--   \i scripts/sql/biz/select_missing_material_distribution.sql
-- 或：
--   psql "$DATABASE_URL" -f scripts/sql/biz/select_missing_material_distribution.sql
--
-- 注意：core.asset 中若有大量「只有 symbol、从无任何 doc_source_entry」的空壳资产，
-- 会拉高所有类型的缺失数。若只想看「真正在跟踪」的资产，取消下方 WHERE 注释。

WITH topics AS (
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
topic_set AS (
    SELECT asset_id, array_agg(DISTINCT topic) AS ts FROM topics GROUP BY asset_id
),
entry_set AS (
    SELECT asset_id, array_agg(DISTINCT entry_type) AS ts
    FROM biz.doc_source_entry WHERE entity_type = 'asset' GROUP BY asset_id
),
doctype_set AS (
    SELECT asset_id, array_agg(DISTINCT doc_type) AS ts
    FROM biz.doc_asset GROUP BY asset_id
),
agg AS (
    SELECT a.asset_id,
           COALESCE(e.ts, '{}'::text[]) AS entry_types,
           COALESCE(d.ts, '{}'::text[]) AS doc_types,
           COALESCE(t.ts, '{}'::text[]) AS topics,
           EXISTS(SELECT 1 FROM biz.asset_tokenomics        x WHERE x.asset_id = a.asset_id) AS has_tokenomics,
           EXISTS(SELECT 1 FROM biz.onchain_holder_snapshot x WHERE x.asset_id = a.asset_id) AS has_onchain,
           EXISTS(SELECT 1 FROM biz.asset_social_heat       x WHERE x.asset_id = a.asset_id) AS has_social,
           EXISTS(SELECT 1 FROM biz.asset_token_unlocks     x WHERE x.asset_id = a.asset_id) AS has_unlocks,
           EXISTS(SELECT 1 FROM core.asset_contract         x WHERE x.asset_id = a.asset_id) AS has_contracts
    FROM core.asset a
    -- 只看有文档入口的资产时取消注释：
    -- WHERE a.asset_id IN (SELECT asset_id FROM biz.doc_source_entry)
    LEFT JOIN entry_set  e USING (asset_id)
    LEFT JOIN doctype_set d USING (asset_id)
    LEFT JOIN topic_set  t USING (asset_id)
),
materials AS (
    SELECT asset_id, '官网'          AS label, ('official_website' = ANY(entry_types)) AS present FROM agg
    UNION ALL SELECT asset_id, '白皮书/文档',
        (('whitepaper_page' = ANY(entry_types)) OR ('docs' = ANY(entry_types)) OR ('docs_portal' = ANY(entry_types))
         OR ('whitepaper' = ANY(doc_types)) OR ('tokenomics' = ANY(doc_types)) OR ('docs' = ANY(doc_types))
         OR ('whitepaper' = ANY(topics)) OR ('docs' = ANY(topics))) FROM agg
    UNION ALL SELECT asset_id, 'GitHub仓库',    ('github' = ANY(entry_types)) FROM agg
    UNION ALL SELECT asset_id, '审计报告',      (('audit' = ANY(doc_types)) OR ('audit' = ANY(topics))) FROM agg
    UNION ALL SELECT asset_id, '代币经济学',    (has_tokenomics OR ('tokenomics' = ANY(doc_types))) FROM agg
    UNION ALL SELECT asset_id, '链上持仓数据',  has_onchain FROM agg
    UNION ALL SELECT asset_id, '社交热度',      has_social FROM agg
    UNION ALL SELECT asset_id, '代币解锁数据',  has_unlocks FROM agg
    UNION ALL SELECT asset_id, '合约地址',      has_contracts FROM agg
    UNION ALL SELECT asset_id, 'TGE/IDO',       ('tge_ido' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, 'LP流动性',      ('lp_liquidity' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '国库/多签',     ('treasury_multisig' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '团队/VC',       ('team_vc' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '路线图',        ('roadmap' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '治理DAO',       ('dao_governance' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '漏洞赏金',      ('bug_bounty' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '交易所上线',    ('exchange_listing' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '竞品对比',      ('competitor' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '重大公告/事件', (('major_event' = ANY(topics)) OR ('announcement' = ANY(topics))) FROM agg
    UNION ALL SELECT asset_id, '第三方评级',    ('third_party_rating' = ANY(topics)) FROM agg
    UNION ALL SELECT asset_id, '链上异常事件',  ('onchain_abnormal' = ANY(topics)) FROM agg
)
SELECT label                                              AS 类型,
       count(*)                                           AS 资产总数,
       count(*) FILTER (WHERE present)                    AS 已收集,
       count(*) FILTER (WHERE NOT present)                AS 缺失数,
       round(100.0 * count(*) FILTER (WHERE NOT present) / count(*), 1) AS 缺失率pct
FROM materials
GROUP BY label
ORDER BY 缺失数 DESC, 缺失率pct DESC;
