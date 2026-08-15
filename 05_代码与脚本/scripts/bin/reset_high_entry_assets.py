"""
重置高条目资产的爬取产物，准备重新爬取。

背景：之前 B2/B3 爬了 github/other 等种子入口，产生大量噪声链接。
现在 B2 已跳过 github/other，重新爬取会干净很多。

1. 找出总条目过多的资产（默认 >200 条）
2. 删除这些资产的所有爬取产物链接（deep_crawl + spa_browser_crawl）
3. 重置原始种子入口的 deep_crawled_at / spa_crawled_at，让 B2/B3 重新爬取

用法：
  python reset_high_entry_assets.py [--execute] [--threshold 200]
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

import time

import psycopg
import psycopg.rows
from crypto_research.config import get_settings

settings = get_settings(require_database=True)

CONNECT_RETRIES = 10
CONNECT_RETRY_DELAY = 6


def _connect_with_retry():
    """连接数据库，带重试。Zeabur PG 会周期性重启导致连接被拒。"""
    last_err = None
    for i in range(CONNECT_RETRIES):
        try:
            return psycopg.connect(settings.database_url, connect_timeout=20)
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  [WARN] 连接失败({i + 1}/{CONNECT_RETRIES}): {str(e)[:80]}")
            time.sleep(CONNECT_RETRY_DELAY)
    raise last_err


def main():
    parser = argparse.ArgumentParser(description="重置高条目资产爬取产物")
    parser.add_argument("--execute", action="store_true", help="实际执行（默认 dry-run）")
    parser.add_argument("--threshold", type=int, default=200, help="总条目数阈值")
    args = parser.parse_args()

    print("=" * 70)
    print("  高条目资产爬取产物重置")
    print(f"  阈值: >{args.threshold} 条")
    print(f"  模式: {'执行' if args.execute else 'dry-run 预览'}")
    print("=" * 70)

    with _connect_with_retry() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE dse.discovered_from LIKE 'deep_crawl:%%') AS deep_crawl_cnt,
                       COUNT(*) FILTER (WHERE dse.discovered_from LIKE 'spa_browser_crawl:%%') AS spa_cnt,
                       COUNT(*) FILTER (WHERE dse.discovered_from NOT LIKE 'deep_crawl:%%'
                                        AND dse.discovered_from NOT LIKE 'spa_browser_crawl:%%') AS original_cnt
                FROM biz.doc_source_entry dse
                JOIN core.asset a ON a.asset_id = dse.asset_id
                WHERE dse.entity_type = 'asset'
                GROUP BY a.asset_id, a.canonical_symbol, a.canonical_name
                HAVING COUNT(*) > %s
                ORDER BY total DESC
                """,
                (args.threshold,),
            )
            assets = [dict(r) for r in cur.fetchall()]

        if not assets:
            print(f"\n✅ 没有资产总条目超过 {args.threshold} 条")
            return

        total_dc = sum(a["deep_crawl_cnt"] for a in assets)
        total_spa = sum(a["spa_cnt"] for a in assets)
        total_orig = sum(a["original_cnt"] for a in assets)

        print(f"\n共 {len(assets)} 个资产")
        print(f"  待删 deep_crawl:        {total_dc:,} 条")
        print(f"  待删 spa_browser_crawl: {total_spa:,} 条")
        print(f"  保留原始种子:           {total_orig:,} 条\n")

        print(f"{'Symbol':>8s}  {'Name':<24s}  {'total':>7s}  {'dc':>6s}  {'spa':>6s}  {'orig':>6s}")
        print("-" * 70)
        for a in assets:
            print(f"{a['canonical_symbol'] or '?':>8s}  "
                  f"{(a['canonical_name'] or '?')[:24]:<24s}  "
                  f"{a['total']:>7,}  "
                  f"{a['deep_crawl_cnt']:>6,}  "
                  f"{a['spa_cnt']:>6,}  "
                  f"{a['original_cnt']:>6,}")

        if not args.execute:
            print(f"\n⚠️  dry-run 模式。加 --execute 执行删除。")
            return

        asset_ids = [a["asset_id"] for a in assets]

        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 删除爬取产物（deep_crawl + spa_browser_crawl）
            cur.execute(
                "DELETE FROM biz.doc_source_entry "
                "WHERE asset_id = ANY(%s) AND entity_type = 'asset' "
                "AND (discovered_from LIKE 'deep_crawl:%%' "
                "     OR discovered_from LIKE 'spa_browser_crawl:%%')",
                (asset_ids,),
            )
            deleted = cur.rowcount

            # 重置原始种子入口的爬取状态，让 B2/B3 重新爬取
            cur.execute(
                "UPDATE biz.doc_source_entry "
                "SET deep_crawled_at = NULL, spa_crawled_at = NULL "
                "WHERE asset_id = ANY(%s) AND entity_type = 'asset' "
                "AND discovered_from NOT LIKE 'deep_crawl:%%' "
                "AND discovered_from NOT LIKE 'spa_browser_crawl:%%'",
                (asset_ids,),
            )
            reset = cur.rowcount

        conn.commit()

        print(f"\n{'=' * 70}")
        print(f"  执行完成")
        print(f"  删除爬取产物: {deleted:,} 条")
        print(f"  重置原始种子: {reset} 条")
        print(f"  影响资产: {len(assets)} 个")
        print(f"  💡 下一步: 运行 B2 深度文档发现（自动循环）重新爬取")
        print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
