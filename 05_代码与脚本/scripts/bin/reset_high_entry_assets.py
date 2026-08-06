"""
重置高条目资产的 deep_crawl 数据，准备重新爬取。

背景：之前 B2 爬了 github/other 种子入口，产生大量噪声链接。
现在 B2 已跳过 github/other，重新爬取会干净很多。

1. 找出 deep_crawl 条目过多的资产（默认 >1000 条）
2. 删除这些资产的所有 deep_crawl 链接
3. 重置原始入口的 deep_crawled_at，让 B2 重新爬取

用法：
  python reset_high_entry_assets.py [--execute] [--threshold 1000]
"""

from __future__ import annotations

import argparse
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg
import psycopg.rows
from crypto_research.config import get_settings

settings = get_settings(require_database=True)


def main():
    parser = argparse.ArgumentParser(description="重置高条目资产 deep_crawl 数据")
    parser.add_argument("--execute", action="store_true", help="实际执行（默认 dry-run）")
    parser.add_argument("--threshold", type=int, default=1000, help="deep_crawl 条目数阈值")
    args = parser.parse_args()

    print("=" * 70)
    print("  高条目资产 deep_crawl 重置")
    print(f"  阈值: >{args.threshold} 条 deep_crawl")
    print(f"  模式: {'执行' if args.execute else 'dry-run 预览'}")
    print("=" * 70)

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 1. 找出超阈值资产
            cur.execute(
                """
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       COUNT(*) FILTER (WHERE dse.discovered_from LIKE 'deep_crawl:%%') AS deep_crawl_cnt,
                       COUNT(*) FILTER (WHERE dse.discovered_from NOT LIKE 'deep_crawl:%%') AS original_cnt
                FROM biz.doc_source_entry dse
                JOIN core.asset a ON a.asset_id = dse.asset_id
                WHERE dse.entity_type = 'asset'
                GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
                HAVING COUNT(*) FILTER (WHERE dse.discovered_from LIKE 'deep_crawl:%%') > %s
                ORDER BY deep_crawl_cnt DESC
                """,
                (args.threshold,),
            )
            assets = [dict(r) for r in cur.fetchall()]

        if not assets:
            print(f"\n✅ 没有资产 deep_crawl 超过 {args.threshold} 条")
            return

        total_deep = sum(a["deep_crawl_cnt"] for a in assets)

        print(f"\n共 {len(assets)} 个资产，合计 {total_deep:,} 条 deep_crawl 链接\n")
        print(f"{'Symbol':>8s}  {'Name':<30s}  {'deep_crawl':>12s}  {'原始入口':>10s}")
        print("-" * 70)
        for a in assets:
            print(f"{a['canonical_symbol'] or '?':>8s}  "
                  f"{a['canonical_name'] or '?':<30s}  "
                  f"{a['deep_crawl_cnt']:>12,}  "
                  f"{a['original_cnt']:>10,}")

        if not args.execute:
            print(f"\n⚠️  dry-run 模式。加 --execute 执行删除。")
            return

        # 2. 执行删除 + 重置
        asset_ids = [a["asset_id"] for a in assets]

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 删除 deep_crawl 链接
            cur.execute(
                "DELETE FROM biz.doc_source_entry "
                "WHERE asset_id = ANY(%s) AND entity_type = 'asset' "
                "AND discovered_from LIKE 'deep_crawl:%%'",
                (asset_ids,),
            )
            deleted = cur.rowcount

            # 重置原始入口的 deep_crawled_at
            cur.execute(
                "UPDATE biz.doc_source_entry "
                "SET deep_crawled_at = NULL "
                "WHERE asset_id = ANY(%s) AND entity_type = 'asset' "
                "AND discovered_from NOT LIKE 'deep_crawl:%%'",
                (asset_ids,),
            )
            reset = cur.rowcount

        conn.commit()

        print(f"\n{'=' * 70}")
        print(f"  执行完成")
        print(f"  删除 deep_crawl 链接: {deleted:,} 条")
        print(f"  重置原始入口 deep_crawled_at: {reset} 个")
        print(f"  影响资产: {len(assets)} 个")
        print(f"  💡 下一步: 运行 B2 深度文档发现（自动循环）重新爬取")
        print(f"{'=' * 70}")


if __name__ == "__main__":
    main()