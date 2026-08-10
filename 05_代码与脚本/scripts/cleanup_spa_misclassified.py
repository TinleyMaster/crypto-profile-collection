"""清理 SPA 误判数据：
1. 找出被回溯扫描误标记为 SPA 的页面（spa_crawled_at 有值，但 B2 之前已有子条目）
2. 清除 spa_crawled_at，删除 SPA 爬虫写入的冗余链接
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg
import psycopg.rows
from crypto_research.config import get_settings

settings = get_settings(require_database=True)

print("=" * 60)
print("  SPA 误判数据清理")
print("=" * 60)

with psycopg.connect(settings.database_url) as conn:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

        # 1. 找出误判的条目：spa_crawled_at 有值，但 B2 之前已爬出子条目
        cur.execute("""
            SELECT dse.entry_id, dse.entry_url, dse.entry_type,
                   dse.spa_crawled_at, dse.deep_crawled_at,
                   a.canonical_symbol,
                   (SELECT COUNT(*) FROM biz.doc_source_entry sub
                    WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                      AND sub.asset_id = dse.asset_id) AS b2_child_count,
                   (SELECT COUNT(*) FROM biz.doc_source_entry sub
                    WHERE sub.discovered_from LIKE 'spa_browser_crawl:%%'
                      AND sub.asset_id = dse.asset_id
                      AND sub.discovered_from LIKE '%%' || substring(dse.entry_url from 1 for 43)) AS spa_child_count
            FROM biz.doc_source_entry dse
            LEFT JOIN core.asset a ON dse.asset_id = a.asset_id
            WHERE dse.spa_crawled_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM biz.doc_source_entry sub
                  WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                    AND sub.asset_id = dse.asset_id
              )
            ORDER BY b2_child_count DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            print("\n✅ 没有需要清理的误判数据")
            raise SystemExit(0)

        print(f"\n共 {len(rows)} 个误判条目（SPA已处理但B2之前已有子条目）:\n")
        print(f"{'Symbol':>8s}  {'entry_type':<18s}  {'B2子条目':>8s}  {'SPA子条目':>8s}  URL")
        print("-" * 90)
        for r in rows:
            print(f"{r['canonical_symbol'] or '?':>8s}  "
                  f"{r['entry_type']:<18s}  "
                  f"{r['b2_child_count']:>8,}  "
                  f"{r['spa_child_count'] or 0:>8,}  "
                  f"{(r['entry_url'] or '')[:50]}")

        total_b2_children = sum(r["b2_child_count"] for r in rows)
        total_spa_children = sum(r["spa_child_count"] or 0 for r in rows)
        print(f"\n合计: {len(rows)} 个误判条目, B2已有 {total_b2_children} 子条目, SPA写入 {total_spa_children} 子条目")

        # 2. 清理
        entry_ids = [r["entry_id"] for r in rows]

        # 清除 spa_crawled_at
        with conn.cursor() as cur2:
            cur2.execute(
                "UPDATE biz.doc_source_entry SET spa_crawled_at = NULL WHERE entry_id = ANY(%s)",
                (entry_ids,),
            )
            cleared = cur2.rowcount

        # 删除 SPA 爬虫写入的链接（精确匹配：discovered_from = 'spa_browser_crawl:' + entry_url前缀）
        deleted_total = 0
        with conn.cursor() as cur2:
            for r in rows:
                url_prefix = (r["entry_url"] or "")[:43]
                if url_prefix:
                    cur2.execute(
                        "DELETE FROM biz.doc_source_entry WHERE discovered_from = %s AND asset_id = (SELECT asset_id FROM biz.doc_source_entry WHERE entry_id = %s)",
                        (f"spa_browser_crawl:{url_prefix}", r["entry_id"]),
                    )
                    deleted_total += cur2.rowcount

        conn.commit()

        print(f"\n{'=' * 60}")
        print(f"  清理完成")
        print(f"  清除 spa_crawled_at: {cleared} 个条目")
        print(f"  删除 SPA 写入链接: {deleted_total} 条")
        print(f"{'=' * 60}")