"""
高条目资产污染溯源：分析文档链接 >1000 的代币，定位污染链路。
"""
from __future__ import annotations

import json
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg
from crypto_research.config import get_settings

settings = get_settings(require_database=True)

ENTRY_THRESHOLD = 1000

print("=" * 70)
print("  高条目资产污染溯源分析")
print(f"  阈值: >{ENTRY_THRESHOLD} 条文档链接")
print("=" * 70)

with psycopg.connect(settings.database_url) as conn:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:

        # 1. 找出所有超阈值资产
        cur.execute("""
            SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                   COUNT(*) AS total_entries
            FROM biz.doc_source_entry dse
            JOIN core.asset a ON a.asset_id = dse.asset_id
            WHERE dse.entity_type = 'asset'
            GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
            HAVING COUNT(*) > %s
            ORDER BY COUNT(*) DESC
        """, (ENTRY_THRESHOLD,))
        assets = [dict(r) for r in cur.fetchall()]

        if not assets:
            print(f"\n✅ 没有资产超过 {ENTRY_THRESHOLD} 条链接")
            raise SystemExit(0)

        print(f"\n共 {len(assets)} 个资产超过阈值:\n")
        for a in assets:
            print(f"  {a['canonical_symbol']:>8s}  {a['canonical_name']:<30s}  {a['total_entries']:>8,} 条")

        # 2. 逐个分析
        print("\n" + "=" * 70)
        print("  逐资产详细分析")
        print("=" * 70)

        for asset in assets:
            aid = asset["asset_id"]
            sym = asset["canonical_symbol"]
            name = asset["canonical_name"]

            print(f"\n{'─' * 70}")
            print(f"  {sym} ({name})  asset_id={aid}  共 {asset['total_entries']:,} 条")
            print(f"{'─' * 70}")

            # 2.1 原始入口 vs deep_crawl
            cur.execute("""
                SELECT
                    CASE WHEN discovered_from LIKE 'deep_crawl:%%' THEN 'deep_crawl' ELSE 'original' END AS origin,
                    COUNT(*) AS cnt
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                GROUP BY 1 ORDER BY cnt DESC
            """, (aid,))
            for row in cur.fetchall():
                print(f"  {'原始入口' if row['origin'] == 'original' else 'deep_crawl'}: {row['cnt']:>8,} 条")

            # 2.2 按 entry_type 分布
            cur.execute("""
                SELECT entry_type, COUNT(*) AS cnt
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                GROUP BY entry_type ORDER BY cnt DESC LIMIT 8
            """, (aid,))
            print(f"  entry_type 分布:")
            for row in cur.fetchall():
                print(f"    {row['entry_type']:<20s} {row['cnt']:>8,}")

            # 2.3 按 source_code 分布
            cur.execute("""
                SELECT source_code, COUNT(*) AS cnt
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                GROUP BY source_code ORDER BY cnt DESC
            """, (aid,))
            print(f"  source_code 分布:")
            for row in cur.fetchall():
                print(f"    {row['source_code']:<20s} {row['cnt']:>8,}")

            # 2.4 原始入口详情（哪些种子入口产出了大量 deep_crawl）
            cur.execute("""
                SELECT entry_type, entry_url, discovered_from,
                       (SELECT COUNT(*) FROM biz.doc_source_entry sub
                        WHERE sub.discovered_from = 'deep_crawl:' || dse.entry_url
                          AND sub.asset_id = dse.asset_id) AS child_count
                FROM biz.doc_source_entry dse
                WHERE asset_id = %s AND entity_type = 'asset'
                  AND discovered_from NOT LIKE 'deep_crawl:%%'
                ORDER BY child_count DESC NULLS LAST
                LIMIT 10
            """, (aid,))
            print(f"  原始入口 → 产出的 deep_crawl 子链接数:")
            for row in cur.fetchall():
                cc = row["child_count"] or 0
                marker = " ⚠️ 污染源" if cc > 100 else (" ✅ 正常" if cc > 0 else "")
                url_short = (row["entry_url"] or "")[:80]
                print(f"    [{row['entry_type']:<18s}] → {cc:>6,} 子链接{marker}")
                print(f"      {url_short}")

            # 2.5 deep_crawl 条目域名 TOP 10
            cur.execute("""
                SELECT
                    SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain,
                    COUNT(*) AS cnt
                FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                  AND discovered_from LIKE 'deep_crawl:%%'
                GROUP BY domain ORDER BY cnt DESC LIMIT 10
            """, (aid,))
            print(f"  deep_crawl 域名 TOP 10:")
            for row in cur.fetchall():
                print(f"    {row['cnt']:>8,}  {row['domain']}")

            # 2.6 污染链路追踪：从种子入口到噪声域名的完整链路
            cur.execute("""
                WITH seed_entries AS (
                    SELECT entry_id, entry_url, entry_type
                    FROM biz.doc_source_entry
                    WHERE asset_id = %s AND entity_type = 'asset'
                      AND discovered_from NOT LIKE 'deep_crawl:%%'
                ),
                deep_entries AS (
                    SELECT entry_id, entry_url, entry_type, discovered_from,
                           SUBSTRING(entry_url FROM 'https?://([^/]+)') AS domain
                    FROM biz.doc_source_entry
                    WHERE asset_id = %s AND entity_type = 'asset'
                      AND discovered_from LIKE 'deep_crawl:%%'
                )
                SELECT
                    s.entry_type AS seed_type,
                    s.entry_url AS seed_url,
                    d.domain,
                    COUNT(*) AS cnt
                FROM deep_entries d
                JOIN seed_entries s ON d.discovered_from = 'deep_crawl:' || s.entry_url
                WHERE d.domain NOT IN ('github.com', 'docs.rs', 'medium.com',
                    'raw.githubusercontent.com', 'gitlab.com')
                GROUP BY s.entry_type, s.entry_url, d.domain
                HAVING COUNT(*) > 10
                ORDER BY cnt DESC
                LIMIT 15
            """, (aid, aid))
            rows = cur.fetchall()
            if rows:
                print(f"  污染链路（种子→噪声域名，>10条）:")
                for row in rows:
                    seed_short = (row["seed_url"] or "")[:70]
                    print(f"    [{row['seed_type']:<16s}] {seed_short}")
                    print(f"      → {row['domain']:<50s} {row['cnt']:>6,} 条")

            # 2.7 诊断结论
            cur.execute("""
                SELECT COUNT(*) FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                  AND discovered_from NOT LIKE 'deep_crawl:%%'
                  AND entry_type IN ('github', 'other')
            """, (aid,))
            github_other_seeds = cur.fetchone()["count"]

            cur.execute("""
                SELECT COUNT(*) FROM biz.doc_source_entry
                WHERE asset_id = %s AND entity_type = 'asset'
                  AND discovered_from LIKE 'deep_crawl:%%'
                  AND SUBSTRING(entry_url FROM 'https?://([^/]+)') NOT IN
                      ('github.com', 'docs.rs', 'medium.com', 'raw.githubusercontent.com',
                       'gitlab.com', 'docs.io.net', 'io.net', 'karura.network',
                       'reflexer.finance', 'alvara.xyz', 'alvaraprotocol.io',
                       'civic.com', 'dignitygold.com', 'dogeyinu.com',
                       'knightswap.finance', 'uncx.network')
            """, (aid,))
            noise_links = cur.fetchone()["count"]

            print(f"  诊断结论:")
            if github_other_seeds > 0:
                print(f"    ⚠️ 有 {github_other_seeds} 个 github/other 种子入口（已被 B2 跳过）")
                print(f"       这些种子以前爬取时产生了大量噪声链接")
            print(f"    📊 估计噪声链接: {noise_links:,} 条（非加密/技术/包管理域名）")
            print(f"    💡 建议: 运行 B2 AI 噪声清理，或等自动循环处理")

print(f"\n{'=' * 70}")
print("  分析完成")
print("=" * 70)