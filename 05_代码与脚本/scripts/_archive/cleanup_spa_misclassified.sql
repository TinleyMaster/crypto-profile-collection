-- ============================================================
-- SPA 误判数据清理 SQL（在服务器上直接执行）
-- ============================================================

-- 1. 查看当前状态
SELECT
    COUNT(*) FILTER (WHERE needs_browser = TRUE) AS pending,
    COUNT(*) FILTER (WHERE spa_crawled_at IS NOT NULL) AS done
FROM biz.doc_source_entry
WHERE entry_type IN ('official_website', 'docs')
  AND (needs_browser = TRUE OR spa_crawled_at IS NOT NULL);

-- 2. 查看误判数量（needs_browser=TRUE 但 B2 已有子条目）
SELECT COUNT(*) AS misclassified_pending
FROM biz.doc_source_entry dse
WHERE dse.needs_browser = TRUE
  AND dse.entry_type IN ('official_website', 'docs')
  AND EXISTS (
      SELECT 1 FROM biz.doc_source_entry sub
      WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
        AND sub.asset_id = dse.asset_id
  );

-- 3. 清理误判：清除 needs_browser 标记
BEGIN;
UPDATE biz.doc_source_entry
SET needs_browser = FALSE
WHERE entry_id IN (
    SELECT dse.entry_id
    FROM biz.doc_source_entry dse
    WHERE dse.needs_browser = TRUE
      AND dse.entry_type IN ('official_website', 'docs')
      AND EXISTS (
          SELECT 1 FROM biz.doc_source_entry sub
          WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
            AND sub.asset_id = dse.asset_id
      )
);
COMMIT;

-- 4. 验证结果（应该为 0）
SELECT COUNT(*) AS remaining
FROM biz.doc_source_entry dse
WHERE dse.needs_browser = TRUE
  AND dse.entry_type IN ('official_website', 'docs')
  AND EXISTS (
      SELECT 1 FROM biz.doc_source_entry sub
      WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
        AND sub.asset_id = dse.asset_id
  );