import psycopg
conn = psycopg.connect("postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto", connect_timeout=15)
cur = conn.cursor()

# 1. 总体统计
cur.execute("""
    SELECT
        COUNT(*) FILTER (WHERE needs_browser = TRUE) AS pending,
        COUNT(*) FILTER (WHERE spa_crawled_at IS NOT NULL) AS done
    FROM biz.doc_source_entry
    WHERE entry_type IN ('official_website', 'docs')
      AND (needs_browser = TRUE OR spa_crawled_at IS NOT NULL)
""")
r = cur.fetchone()
print(f"B3 SPA 总体: pending={r[0]} done={r[1]}")

# 2. needs_browser=TRUE 且 B2 有子条目（误标记待处理）
cur.execute("""
    SELECT COUNT(*) FROM biz.doc_source_entry dse
    WHERE dse.needs_browser = TRUE
      AND dse.entry_type IN ('official_website', 'docs')
      AND EXISTS (
          SELECT 1 FROM biz.doc_source_entry sub
          WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
            AND sub.asset_id = dse.asset_id
      )
""")
print(f"needs_browser=TRUE 且 B2有子条目（误判待处理）: {cur.fetchone()[0]}")

# 3. spa_crawled_at有值 且 B2 有子条目（误判已处理）
cur.execute("""
    SELECT COUNT(*) FROM biz.doc_source_entry dse
    WHERE dse.spa_crawled_at IS NOT NULL
      AND dse.entry_type IN ('official_website', 'docs')
      AND EXISTS (
          SELECT 1 FROM biz.doc_source_entry sub
          WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
            AND sub.asset_id = dse.asset_id
      )
""")
print(f"spa_crawled_at有值 且 B2有子条目（误判已处理）: {cur.fetchone()[0]}")

# 4. retro_scan_checked_at 且 needs_browser=TRUE 的总数
cur.execute("""
    SELECT COUNT(*) FROM biz.doc_source_entry
    WHERE retro_scan_checked_at IS NOT NULL
      AND needs_browser = TRUE
      AND entry_type IN ('official_website', 'docs')
""")
print(f"回溯扫描标记 needs_browser=TRUE 总数: {cur.fetchone()[0]}")

conn.close()