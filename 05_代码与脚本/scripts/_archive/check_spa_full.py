"""全面检查 SPA 误判数据"""
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
print("  SPA 误判数据全面检查")
print("=" * 60)

with psycopg.connect(settings.database_url) as conn:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

        # 1. 总体统计
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE needs_browser = TRUE) AS pending,
                COUNT(*) FILTER (WHERE spa_crawled_at IS NOT NULL) AS done,
                COUNT(*) FILTER (WHERE retro_scan_checked_at IS NOT NULL) AS retro_scanned
            FROM biz.doc_source_entry
            WHERE entry_type IN ('official_website', 'docs')
              AND (needs_browser = TRUE OR spa_crawled_at IS NOT NULL OR retro_scan_checked_at IS NOT NULL)
        """)
        stats = cur.fetchone()
        print(f"\n总体统计:")
        print(f"  needs_browser=TRUE (待处理): {stats['pending']}")
        print(f"  spa_crawled_at IS NOT NULL (已处理): {stats['done']}")
        print(f"  retro_scan_checked_at IS NOT NULL (回溯扫描过): {stats['retro_scanned']}")

        # 2. needs_browser=TRUE 但 B2 已有子条目 → 误标记但未处理
        print(f"\n{'=' * 60}")
        print("  [误判] needs_browser=TRUE 但 B2 已有子条目（待处理）")
        print("=" * 60)
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
        rows1 = [dict(r) for r in cur.fetchall()]
        if rows1:
            for r in rows1:
                print(f"  {r['canonical_symbol'] or '?':>8s}  {r['entry_type']:<18s}  B2子条目={r['b2_child_count']}  {(r['entry_url'] or '')[:60]}")
        else:
            print("  (无)")

        # 3. spa_crawled_at IS NOT NULL 但 B2 已有子条目 → 误判已处理（已清理）
        print(f"\n{'=' * 60}")
        print("  [误判] spa_crawled_at IS NOT NULL 但 B2 已有子条目（已处理）")
        print("=" * 60)
        cur.execute("""
            SELECT dse.entry_id, dse.entry_url, dse.entry_type,
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
        rows2 = [dict(r) for r in cur.fetchall()]
        if rows2:
            for r in rows2:
                print(f"  {r['canonical_symbol'] or '?':>8s}  {r['entry_type']:<18s}  B2子条目={r['b2_child_count']}  {(r['entry_url'] or '')[:60]}")
        else:
            print("  (无)")

        # 4. retro_scan_checked_at IS NOT NULL 且 needs_browser=FALSE 且 spa_crawled_at IS NULL
        #    回溯扫描检查过，判定为非SPA，但有B2子条目 → 正常
        print(f"\n{'=' * 60}")
        print("  [正常] 回溯扫描过，判定非SPA，B2有子条目")
        print("=" * 60)
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM biz.doc_source_entry dse
            WHERE dse.retro_scan_checked_at IS NOT NULL
              AND COALESCE(dse.needs_browser, FALSE) = FALSE
              AND dse.spa_crawled_at IS NULL
              AND dse.entry_type IN ('official_website', 'docs')
        """)
        print(f"  共 {cur.fetchone()['cnt']} 个")

        # 5. 总结：所有 retro_scan_checked_at 的条目中，标记为 needs_browser=TRUE 的有多少
        print(f"\n{'=' * 60}")
        print("  回溯扫描标记为 SPA 的条目")
        print("=" * 60)
        cur.execute("""
            SELECT COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE spa_crawled_at IS NOT NULL) AS done,
                   COUNT(*) FILTER (WHERE spa_crawled_at IS NULL) AS pending
            FROM biz.doc_source_entry
            WHERE retro_scan_checked_at IS NOT NULL
              AND needs_browser = TRUE
              AND entry_type IN ('official_website', 'docs')
        """)
        row = cur.fetchone()
        print(f"  回溯扫描标记 needs_browser=TRUE: {row['cnt']} 个")
        print(f"    其中已处理(spa_crawled_at有值): {row['done']}")
        print(f"    其中待处理(spa_crawled_at为空): {row['pending']}")

        # 6. 这些被标记的条目中，有多少有 B2 子条目（即误判）
        print(f"\n{'=' * 60}")
        print("  回溯扫描标记SPA的条目中，有B2子条目的（=误判）")
        print("=" * 60)
        cur.execute("""
            SELECT dse.entry_id, dse.entry_url, dse.entry_type,
                   dse.needs_browser, dse.spa_crawled_at,
                   a.canonical_symbol,
                   (SELECT COUNT(*) FROM biz.doc_source_entry sub
                    WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                      AND sub.asset_id = dse.asset_id) AS b2_child_count
            FROM biz.doc_source_entry dse
            LEFT JOIN core.asset a ON dse.asset_id = a.asset_id
            WHERE dse.retro_scan_checked_at IS NOT NULL
              AND dse.needs_browser = TRUE
              AND dse.entry_type IN ('official_website', 'docs')
              AND EXISTS (
                  SELECT 1 FROM biz.doc_source_entry sub
                  WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                    AND sub.asset_id = dse.asset_id
              )
            ORDER BY b2_child_count DESC
        """)
        rows6 = [dict(r) for r in cur.fetchall()]
        if rows6:
            for r in rows6:
                status = "已处理" if r["spa_crawled_at"] else "待处理"
                print(f"  [{status}] {r['canonical_symbol'] or '?':>8s}  {r['entry_type']:<18s}  B2子条目={r['b2_child_count']}  {(r['entry_url'] or '')[:60]}")
        else:
            print("  (无)")

        print(f"\n{'=' * 60}")
        print("  检查完成")
        print("=" * 60)