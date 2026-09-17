"""地址标签批量富化 — 本地回填脚本。

从转账记录表中捞出没有标签的陌生地址，去区块浏览器爬取标签，
写入 onchain_address_label 并回填 onchain_transfer_log。

设计目的：服务器 IP 被 Cloudflare 拦截，无法在服务器上实时爬取，
因此在本地（IP 正常）批量跑，把结果写回数据库。

支持增量：只查 onchain_transfer_log 中出现过、但 onchain_address_label 中没有的地址。
幂等：已有标签的地址跳过，可重复执行。

用法：
    # 预览（不爬取、不写入，只看有多少陌生地址）
    python backfill_enrich_labels.py --dry-run --chain eth

    # 实际执行（默认 eth 链，每次最多 100 个地址）
    python backfill_enrich_labels.py --chain eth --limit 100

    # 多条链一起跑
    python backfill_enrich_labels.py --chain eth,base,polygon --limit 200
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.explorer_label_fetcher import ExplorerLabelFetcher
from crypto_research.clients.label_enricher import LabelEnricher, ENRICH_SOURCE, ALLOWED_LABEL_TYPES
from crypto_research.clients.address_label_resolver import AddressLabelResolver

# 支持 HTML 爬取的链（Etherscan 系列）
ENRICH_SUPPORTED_CHAINS = {"eth", "base", "polygon"}
# 大小写敏感链
CASE_SENSITIVE_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}


def get_unlabeled_addresses(conn, chain: str, limit: int) -> list[str]:
    """从转账记录中捞出没有地址标签的地址（按出现频次倒序，优先爬高频地址）。

    只查 from_address / to_address 中出现过、但 onchain_address_label 中没有的地址。
    """
    case_sensitive = chain in CASE_SENSITIVE_CHAINS

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if case_sensitive:
            addr_col_from = "from_address"
            addr_col_to = "to_address"
            compare = "="
        else:
            addr_col_from = "LOWER(from_address)"
            addr_col_to = "LOWER(to_address)"
            compare = "="

        cur.execute(f"""
            WITH all_addrs AS (
                SELECT {addr_col_from} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s
                  AND from_address IS NOT NULL
                UNION ALL
                SELECT {addr_col_to} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s
                  AND to_address IS NOT NULL
            ),
            addr_counts AS (
                SELECT addr, COUNT(*) AS cnt
                FROM all_addrs
                WHERE addr IS NOT NULL AND addr <> ''
                GROUP BY addr
            ),
            labeled AS (
                SELECT address AS addr
                FROM biz.onchain_address_label
                WHERE chain = %s
                UNION
                SELECT address AS addr
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s
            )
            SELECT ac.addr, ac.cnt
            FROM addr_counts ac
            LEFT JOIN labeled l
              ON {'LOWER(ac.addr)' if not case_sensitive else 'ac.addr'} {compare}
                 {'LOWER(l.addr)' if not case_sensitive else 'l.addr'}
            WHERE l.addr IS NULL
            ORDER BY ac.cnt DESC
            LIMIT %s
        """, (chain, chain, chain, chain, limit))

        rows = cur.fetchall()

    return [r["addr"] for r in rows]


def count_total_unlabeled(conn, chain: str) -> int:
    """估算总共有多少个无标签地址（用于预览）。"""
    case_sensitive = chain in CASE_SENSITIVE_CHAINS

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if case_sensitive:
            addr_col_from = "from_address"
            addr_col_to = "to_address"
            compare = "="
        else:
            addr_col_from = "LOWER(from_address)"
            addr_col_to = "LOWER(to_address)"
            compare = "="

        cur.execute(f"""
            WITH all_addrs AS (
                SELECT DISTINCT {addr_col_from} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND from_address IS NOT NULL
                UNION
                SELECT DISTINCT {addr_col_to} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND to_address IS NOT NULL
            ),
            labeled AS (
                SELECT LOWER(address) AS addr
                FROM biz.onchain_address_label
                WHERE chain = %s
                UNION
                SELECT LOWER(address) AS addr
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s
            )
            SELECT COUNT(*) AS cnt
            FROM all_addrs ac
            LEFT JOIN labeled l ON LOWER(ac.addr) = LOWER(l.addr)
            WHERE l.addr IS NULL AND ac.addr IS NOT NULL AND ac.addr <> ''
        """, (chain, chain, chain, chain))

        row = cur.fetchone()
    return row["cnt"] if row else 0


def main():
    parser = argparse.ArgumentParser(description="批量富化地址标签（从区块浏览器爬取并写回 DB）")
    parser.add_argument("--chain", type=str, default="eth",
                        help=f"链名，多个用逗号分隔（支持: {', '.join(sorted(ENRICH_SUPPORTED_CHAINS))}）")
    parser.add_argument("--limit", type=int, default=100,
                        help="每条链最多爬多少个地址（默认 100，优先爬高频地址）")
    parser.add_argument("--batch-size", type=int, default=20,
                        help="每批爬多少个地址后写库（默认 20）")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="每个请求的间隔秒数（默认 1.5 秒，礼貌限速）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式：只统计，不爬取、不写库")
    parser.add_argument("--db-url", type=str, default=None,
                        help="数据库连接串（默认从 settings 或 DATABASE_URL 环境变量读）")
    args = parser.parse_args()

    chains = [c.strip() for c in args.chain.split(",") if c.strip()]
    invalid = [c for c in chains if c not in ENRICH_SUPPORTED_CHAINS]
    if invalid:
        print(f"错误：不支持的链: {', '.join(invalid)}。支持的链: {', '.join(sorted(ENRICH_SUPPORTED_CHAINS))}")
        sys.exit(1)

    # 连接 DB 并执行
    if args.db_url:
        with psycopg.connect(args.db_url) as conn:
            _run_for_chains(conn, chains, args)
    else:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            _run_for_chains(conn, chains, args)

    print("\n全部完成。")


def _run_for_chains(conn, chains: list[str], args) -> None:
    """对多条链依次执行富化。"""
    for chain in chains:
        print(f"\n{'=' * 60}")
        print(f"链: {chain}")
        print(f"{'=' * 60}")

        # 先预览总量
        total_unlabeled = count_total_unlabeled(conn, chain)
        print(f"  无标签地址总数（估算）: {total_unlabeled}")
        print(f"  本次计划爬取: {min(args.limit, total_unlabeled)} 个")

        if args.dry_run:
            print("  [dry-run] 跳过实际爬取")
            continue

        if total_unlabeled == 0:
            print("  没有需要富化的地址，跳过")
            continue

        # 捞出待爬地址
        addresses = get_unlabeled_addresses(conn, chain, args.limit)
        if not addresses:
            print("  没有找到待富化地址")
            continue

        print(f"  捞出 {len(addresses)} 个待爬地址（按频次排序）")
        print(f"  前 5 个: {addresses[:5]}")

        # 初始化 fetcher + enricher
        resolver = AddressLabelResolver(conn, chain)
        fetcher = ExplorerLabelFetcher(chain=chain, delay=args.delay)
        enricher = LabelEnricher(
            conn, chain,
            fetcher=fetcher,
            resolver=resolver,
            max_batch_size=args.batch_size,
            dry_run=args.dry_run,
            scrape=True,
        )

        total_fetched = 0
        total_inserted = 0
        total_backfilled = 0
        total_no_label = 0
        total_failed = 0
        t0 = time.time()

        # 分批爬取 + 写库
        for i in range(0, len(addresses), args.batch_size):
            batch = addresses[i:i + args.batch_size]
            enricher.collect(batch)
            stats = enricher.flush()

            total_fetched += stats.get("fetched", 0)
            total_inserted += stats.get("inserted", 0)
            total_backfilled += stats.get("backfilled", 0)
            total_no_label += stats.get("no_label", 0)
            total_failed += stats.get("fetch_failed", 0)

            progress = min(i + args.batch_size, len(addresses))
            pct = progress / len(addresses) * 100

            fail_detail = ""
            if stats.get("fetch_failed", 0) > 0:
                fd = stats.get("fetch_detail", {})
                parts = []
                if fd.get("http_403", 0):
                    parts.append(f"403×{fd['http_403']}")
                if fd.get("http_429", 0):
                    parts.append(f"429×{fd['http_429']}")
                if fd.get("network_error", 0):
                    parts.append(f"网络×{fd['network_error']}")
                if parts:
                    fail_detail = f"（失败: {', '.join(parts)}）"

            print(f"  [{progress}/{len(addresses)}] {pct:.0f}% | "
                  f"本批查到标签 {stats.get('fetched', 0)} 个, "
                  f"入库 {stats.get('inserted', 0)} 条, "
                  f"回填 {stats.get('backfilled', 0)} 条{fail_detail}")

        enricher.close()

        elapsed = time.time() - t0
        print(f"\n  ── {chain} 完成 ──")
        print(f"    总耗时: {elapsed:.1f}s")
        print(f"    爬取地址: {len(addresses)} 个")
        print(f"    查到标签: {total_fetched} 个")
        print(f"    无标签: {total_no_label} 个")
        print(f"    爬取失败: {total_failed} 个")
        print(f"    入库新标签: {total_inserted} 条")
        print(f"    回填转账记录: {total_backfilled} 条")


if __name__ == "__main__":
    main()
