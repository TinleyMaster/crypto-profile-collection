-- ============================================================
-- 审计报告 2026-08-24 修复迁移
-- FIX-A: GitHub 链接漏标批量修复
-- FIX-B: 白皮书链接批量重标
-- FIX-C: TVL 落库（src_dl.protocol_list → biz.protocol_metric_daily）
-- FIX-D: research_thesis 重复行清理
--
-- 执行前建议先 pg_dump 备份
-- ============================================================

BEGIN;

-- ============================================================
-- FIX-A: GitHub 链接漏标修复
-- URL 含 github.com 且 entry_type 不是 github 的，批量重标
-- ============================================================
UPDATE biz.doc_source_entry
SET entry_type = 'github', updated_at = NOW()
WHERE entry_url ILIKE '%github.com%'
  AND entry_type <> 'github';

-- 验证
SELECT 'FIX-A github' AS fix, COUNT(*) AS total
FROM biz.doc_source_entry WHERE entry_type = 'github';

-- ============================================================
-- FIX-B: 白皮书链接批量重标
-- URL 含 whitepaper/litepaper 关键词且当前为 docs/docs_portal/other 的，重标为 whitepaper_page
-- ============================================================
UPDATE biz.doc_source_entry
SET entry_type = 'whitepaper_page', updated_at = NOW()
WHERE entry_type IN ('docs', 'docs_portal', 'other')
  AND (
    entry_url ILIKE '%whitepaper%'
    OR entry_url ILIKE '%litepaper%'
    OR entry_url ILIKE '%white-paper%'
    OR entry_url ILIKE '%lite-paper%'
  );

-- 验证
SELECT 'FIX-B whitepaper' AS fix, COUNT(*) AS total
FROM biz.doc_source_entry WHERE entry_type = 'whitepaper_page';

-- ============================================================
-- FIX-C: TVL 落库
-- 从 src_dl.protocol_list 读取 TVL 快照，映射 asset_id 后写入 biz.protocol_metric_daily
-- ============================================================

-- C1: 确保 source_platform 注册
INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
VALUES ('dl', 'DefiLlama', 'https://defillama.com', 'DefiLlama TVL 数据', TRUE)
ON CONFLICT (platform_code) DO NOTHING;

-- C2: 创建 biz.protocol_metric_daily 表
CREATE TABLE IF NOT EXISTS biz.protocol_metric_daily (
    asset_id      INTEGER NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    metric_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    tvl           NUMERIC(20, 2),
    tvl_change_1d NUMERIC,
    tvl_change_7d NUMERIC,
    source_code   TEXT NOT NULL DEFAULT 'dl',
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, metric_date, source_code)
);

CREATE INDEX IF NOT EXISTS idx_protocol_metric_daily_date
    ON biz.protocol_metric_daily (metric_date DESC);

-- C3: 从 src_dl.protocol_list 聚合写入
INSERT INTO biz.protocol_metric_daily (asset_id, metric_date, tvl, tvl_change_1d, tvl_change_7d, source_code)
SELECT
    m.asset_id,
    CURRENT_DATE,
    p.tvl,
    p.change_1d,
    p.change_7d,
    'dl'
FROM src_dl.protocol_list p
JOIN core.asset_source_map m
    ON m.source_code = 'dl' AND m.source_asset_key = p.protocol_id
WHERE p.tvl IS NOT NULL AND p.tvl > 0
ON CONFLICT (asset_id, metric_date, source_code) DO UPDATE SET
    tvl = EXCLUDED.tvl,
    tvl_change_1d = EXCLUDED.tvl_change_1d,
    tvl_change_7d = EXCLUDED.tvl_change_7d,
    updated_at = NOW();

-- 验证
SELECT 'FIX-C tvl' AS fix, COUNT(*) AS total, COUNT(DISTINCT asset_id) AS assets
FROM biz.protocol_metric_daily;

-- ============================================================
-- FIX-D: research_thesis 重复行清理
-- 保留每 (asset_id, source_notebook_id) 最新一条，删除旧的
-- ============================================================
DELETE FROM biz.research_thesis
WHERE thesis_id IN (
    SELECT thesis_id
    FROM (
        SELECT thesis_id,
               ROW_NUMBER() OVER (
                   PARTITION BY asset_id, source_notebook_id
                   ORDER BY updated_at DESC, thesis_id DESC
               ) AS rn
        FROM biz.research_thesis
    ) t
    WHERE rn > 1
);

-- D2: 添加唯一约束（如果还没有）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_research_thesis_asset_notebook'
    ) THEN
        ALTER TABLE biz.research_thesis
        ADD CONSTRAINT uq_research_thesis_asset_notebook
        UNIQUE (asset_id, source_notebook_id);
    END IF;
END $$;

-- 验证
SELECT 'FIX-D thesis' AS fix, COUNT(*) AS total, COUNT(DISTINCT asset_id) AS assets
FROM biz.research_thesis;

COMMIT;

-- ============================================================
-- 最终验证（COMMIT 后执行）
-- ============================================================
SELECT 'github 标注率' AS metric,
    COUNT(*) FILTER (WHERE entry_type = 'github') AS github_count,
    COUNT(*) FILTER (WHERE entry_url ILIKE '%github.com%') AS url_with_github,
    ROUND(
        COUNT(*) FILTER (WHERE entry_type = 'github')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE entry_url ILIKE '%github.com%'), 0) * 100
    , 1) AS pct
FROM biz.doc_source_entry;

SELECT 'whitepaper 标注率' AS metric,
    COUNT(*) FILTER (WHERE entry_type = 'whitepaper_page') AS wp_count,
    COUNT(*) FILTER (WHERE entry_url ILIKE '%whitepaper%' OR entry_url ILIKE '%litepaper%') AS url_with_wp
FROM biz.doc_source_entry;

SELECT 'TVL 资产数' AS metric, COUNT(DISTINCT asset_id) AS assets, SUM(tvl) AS total_tvl
FROM biz.protocol_metric_daily WHERE metric_date = CURRENT_DATE;

SELECT 'thesis 去重后' AS metric, COUNT(*) AS rows, COUNT(DISTINCT asset_id) AS assets
FROM biz.research_thesis;
