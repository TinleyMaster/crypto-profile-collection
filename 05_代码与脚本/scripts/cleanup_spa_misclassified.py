"""清理 SPA 误判数据：
1. 找出被回溯扫描误标记为 SPA 的页面（B2 已有子条目，说明 B2 正常爬取成功）
2. 已处理（spa_crawled_at有值）：清除 spa_crawled_at，删除 SPA 爬虫写入的冗余链接
3. 待处理（needs_browser=TRUE）：清除 needs_browser 标记
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import psycopg.rows

DB_URL = "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto"

print("=" * 60)
print("  SPA 误判数据清理")
print("=" * 60)

with psycopg.connect(DB_URL, connect_timeout=15) as conn:
    total_cleared = 0
    total_deleted = 0

    # === 第1轮：已处理的误判（spa_crawled_at 有值）===
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT dse.entry_id, dse.entry_url, dse.entry_type,
                   dse.spa_crawled_at,
                   a.canonical_symbol,
                   (SELECT COUNT(*) FROM biz.doc_source_entry sub
                    WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                      AND sub.asset_id = dse.asset_id) AS b2_child_count
            FROM biz.doc_source_entry dse
            LEFT JOIN core.asset a ON dse.asset_id = a.asset_id
            WHERE dse.spa_crawled_at IS NOT NULL
              AND dse.entry_type IN ('official_website', 'docs')
              AND EXISTS (
                  SELECT 1 FROM biz.doc_source_entry sub
                  WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                    AND sub.asset_id = dse.asset_id
              )
            ORDER BY b2_child_count DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]

    if rows:
        print(f"\n[第1轮] 已处理误判（spa_crawled_at有值）: {len(rows)} 个")
        for r in rows[:10]:
            print(f"  {r['canonical_symbol'] or '?':>8s}  B2子条目={r['b2_child_count']}  {(r['entry_url'] or '')[:50]}")
        if len(rows) > 10:
            print(f"  ... 共 {len(rows)} 个")

        entry_ids = [r["entry_id"] for r in rows]
        with conn.cursor() as cur2:
            cur2.execute(
                "UPDATE biz.doc_source_entry SET spa_crawled_at = NULL WHERE entry_id = ANY(%s)",
                (entry_ids,),
            )
            total_cleared += cur2.rowcount

        deleted = 0
        with conn.cursor() as cur2:
            for r in rows:
                url_prefix = (r["entry_url"] or "")[:43]
                if url_prefix:
                    cur2.execute(
                        "DELETE FROM biz.doc_source_entry WHERE discovered_from = %s AND asset_id = (SELECT asset_id FROM biz.doc_source_entry WHERE entry_id = %s)",
                        (f"spa_browser_crawl:{url_prefix}", r["entry_id"]),
                    )
                    deleted += cur2.rowcount
        total_deleted += deleted
        print(f"  清除 spa_crawled_at: {total_cleared}, 删除 SPA 链接: {deleted}")
    else:
        print(f"\n[第1轮] 已处理误判: 0 个")

    # === 第2轮：待处理的误判（needs_browser=TRUE）===
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT dse.entry_id, dse.entry_url, dse.entry_type,
                   a.canonical_symbol,
                   (SELECT COUNT(*) FROM biz.doc_source_entry sub
                    WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                      AND sub.asset_id = dse.asset_id) AS b2_child_count
            FROM biz.doc_source_entry dse
            LEFT JOIN core.asset a ON dse.asset_id = a.asset_id
            WHERE dse.needs_browser = TRUE
              AND dse.entry_type IN ('official_website', 'docs')
              AND EXISTS (
                  SELECT 1 FROM biz.doc_source_entry sub
                  WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                    AND sub.asset_id = dse.asset_id
              )
            ORDER BY b2_child_count DESC
        """)
        rows2 = [dict(r) for r in cur.fetchall()]

    if rows2:
        print(f"\n[第2轮] 待处理误判（needs_browser=TRUE）: {len(rows2)} 个")
        for r in rows2[:10]:
            print(f"  {r['canonical_symbol'] or '?':>8s}  B2子条目={r['b2_child_count']}  {(r['entry_url'] or '')[:50]}")
        if len(rows2) > 10:
            print(f"  ... 共 {len(rows2)} 个")

        entry_ids = [r["entry_id"] for r in rows2]
        with conn.cursor() as cur2:
            cur2.execute(
                "UPDATE biz.doc_source_entry SET needs_browser = FALSE WHERE entry_id = ANY(%s)",
                (entry_ids,),
            )
            cleared2 = cur2.rowcount
        total_cleared += cleared2
        print(f"  清除 needs_browser: {cleared2}")
    else:
        print(f"\n[第2轮] 待处理误判: 0 个")

    conn.commit()

    print(f"\n{'=' * 60}")
    print(f"  清理完成")
    print(f"  总计清除标记: {total_cleared} 个条目")
    print(f"  总计删除 SPA 链接: {total_deleted} 条")
    print(f"{'=' * 60}")