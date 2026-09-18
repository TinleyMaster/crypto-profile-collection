"""地址标签批量富化 — 本地回填脚本。

从转账记录表中捞出没有标签的陌生地址，去区块浏览器爬取标签，
写入 onchain_address_label 并回填 onchain_transfer_log。

设计目的：服务器 IP 被 Cloudflare 拦截，无法在服务器上实时爬取，
因此在本地（IP 正常）批量跑，把结果写回数据库。

支持增量：只查 onchain_transfer_log 中出现过、但 onchain_address_label 中没有的地址。
幂等：已有标签的地址跳过，可重复执行。
支持并发：ThreadPoolExecutor 多线程并发爬取，默认 5 线程。

用法：
    # 预览（不爬取、不写入，只看有多少陌生地址）
    python backfill_enrich_labels.py --dry-run --chain eth

    # 实际执行（默认 eth 链，5 并发，每次最多 100 个地址）
    python backfill_enrich_labels.py --chain eth --limit 100

    # 高并发跑全量（10 线程，注意别太猛被封）
    python backfill_enrich_labels.py --chain eth --limit 0 --concurrency 10

    # 多条链一起跑
    python backfill_enrich_labels.py --chain eth,base,polygon --limit 200
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

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
    parser.add_argument("--batch-size", type=int, default=50,
                        help="每多少个地址写库一次（默认 50，并发模式下建议等于或大于 concurrency）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每个请求的最小间隔秒数（默认 0.5 秒，并发模式下为每线程延迟）")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="并发爬取线程数（默认 5，建议不超过 10 避免触发反爬）")
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
    """对多条链依次执行富化（并发爬取 + 批量写库）。"""
    for chain in chains:
        print(f"\n{'=' * 60}")
        print(f"链: {chain}")
        print(f"{'=' * 60}")

        # 先预览总量
        total_unlabeled = count_total_unlabeled(conn, chain)
        print(f"  无标签地址总数（估算）: {total_unlabeled}")
        effective_limit = args.limit if args.limit > 0 else total_unlabeled
        print(f"  本次计划爬取: {min(effective_limit, total_unlabeled)} 个")
        print(f"  并发数: {args.concurrency}")

        if args.dry_run:
            print("  [dry-run] 跳过实际爬取")
            continue

        if total_unlabeled == 0:
            print("  没有需要富化的地址，跳过")
            continue

        # 捞出待爬地址
        addresses = get_unlabeled_addresses(conn, chain, effective_limit)
        if not addresses:
            print("  没有找到待富化地址")
            continue

        print(f"  捞出 {len(addresses)} 个待爬地址（按频次排序）")
        print(f"  前 5 个: {addresses[:5]}")

        # 初始化 resolver（DB 查询用，单线程安全）
        resolver = AddressLabelResolver(conn, chain)

        # 统计变量（多线程共享，用锁保护）
        stats_lock = Lock()
        result_map: dict[str, dict] = {}  # addr -> label_info
        stat_counts = {
            "ok": 0, "no_label": 0,
            "http_403": 0, "http_429": 0, "http_other": 0, "network_error": 0,
        }
        inserted_total = 0
        backfilled_total = 0
        done_count = 0
        t0 = time.time()

        # 并发爬取函数：每个线程一个 fetcher（requests 非线程安全）
        def _fetch_one(addr: str, fetcher: ExplorerLabelFetcher):
            info, status = fetcher.fetch_with_status(addr)
            return addr, info, status

        # 用线程局部变量存每个线程的 fetcher
        thread_local = {}

        def _worker(addr: str):
            # 每个线程创建自己的 fetcher
            thread_id = _thread_id()
            if thread_id not in thread_local:
                thread_local[thread_id] = ExplorerLabelFetcher(
                    chain=chain, delay=args.delay)
            fetcher = thread_local[thread_id]
            return _fetch_one(addr, fetcher)

        # 分批提交 + 每批写完库再下一批（内存可控 + 避免连接池打爆）
        batch_size = args.batch_size
        for batch_start in range(0, len(addresses), batch_size):
            batch = addresses[batch_start:batch_start + batch_size]
            batch_results: dict[str, dict] = {}
            batch_stats = {k: 0 for k in stat_counts}

            # 并发爬取本批
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {pool.submit(_worker, addr): addr for addr in batch}
                for future in as_completed(futures):
                    addr, info, status = future.result()
                    with stats_lock:
                        done_count += 1
                        if info:
                            batch_results[addr.lower()] = info
                            batch_stats["ok"] += 1
                        elif status in batch_stats:
                            batch_stats[status] += 1
                        else:
                            batch_stats["no_label"] += 1

                    # 进度输出（每 10 个打一次）
                    if done_count % max(10, args.concurrency * 2) == 0 or done_count == len(addresses):
                        pct = done_count / len(addresses) * 100
                        elapsed = time.time() - t0
                        rate = done_count / elapsed if elapsed > 0 else 0
                        eta = (len(addresses) - done_count) / rate if rate > 0 else 0
                        print(f"  进度: {done_count}/{len(addresses)} ({pct:.0f}%) | "
                              f"已查到标签 {stat_counts['ok'] + batch_stats['ok']} 个 | "
                              f"速度 {rate:.1f}/s | 预计剩余 {eta/60:.1f} 分钟")

            # 本批写库统计
            batch_inserted = 0
            batch_backfilled = 0

            # 本批写库（单线程，串行安全）
            if batch_results:
                try:
                    # 构造 label_map 格式给 enricher 用
                    label_map = batch_results
                    # 直接用 enricher 的写库逻辑
                    from crypto_research.clients.label_enricher import (
                        ENRICH_CONFIDENCE, ENRICH_SOURCE, ALLOWED_LABEL_TYPES,
                    )
                    import json

                    filtered = {
                        a: info for a, info in label_map.items()
                        if info.get("label_type") in ALLOWED_LABEL_TYPES
                    }

                    if filtered:
                        inserted = 0
                        with conn.cursor() as cur:
                            for addr_l, info in filtered.items():
                                raw_meta = json.dumps({
                                    "label_text": info["label_text"],
                                    "fetched_from": "address_page",
                                }, ensure_ascii=False)
                                cur.execute("""
                                    INSERT INTO biz.onchain_address_label
                                        (address, chain, label_type, label_name, display_name,
                                         confidence, source, raw_meta)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
                                """, (
                                    addr_l, chain, info["label_type"],
                                    info["display_name"], info["display_name"],
                                    ENRICH_CONFIDENCE, ENRICH_SOURCE, raw_meta,
                                ))
                                if cur.rowcount:
                                    inserted += 1

                            # 也写 exchange_wallet
                            for addr_l, info in filtered.items():
                                if not info["is_exchange"]:
                                    continue
                                cur.execute("""
                                    INSERT INTO biz.onchain_exchange_wallet
                                        (address, exchange_name, chain, label, confidence, source)
                                    VALUES (%s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (address, chain) DO NOTHING
                                """, (
                                    addr_l, info["display_name"], chain,
                                    info["label_text"], ENRICH_CONFIDENCE, ENRICH_SOURCE,
                                ))

                        conn.commit()
                        inserted_total += inserted
                        batch_inserted = inserted

                        # 回填转账记录
                        from crypto_research.clients.label_enricher import CASE_SENSITIVE_CHAINS as CS_CHAINS
                        case_sensitive = chain in CS_CHAINS

                        with conn.cursor() as cur:
                            # 刷新 resolver 缓存
                            resolver._no_label -= set(filtered.keys())
                            addr_list = list(filtered.keys())
                            resolver.resolve_batch(addr_list)

                            # 临时表方式回填
                            cur.execute("""
                                CREATE TEMP TABLE tmp_enrich_backfill (
                                    address TEXT PRIMARY KEY,
                                    label_types TEXT[],
                                    label_names TEXT[]
                                ) ON COMMIT DROP
                            """)
                            rows = []
                            for a in addr_list:
                                info = resolver.resolve(a)
                                if info["types"]:
                                    rows.append((a, info["types"], info["names"]))
                            if rows:
                                cur.executemany("""
                                    INSERT INTO tmp_enrich_backfill (address, label_types, label_names)
                                    VALUES (%s, %s, %s)
                                """, rows)

                            # 更新发件方
                            if case_sensitive:
                                from_cond = "t.from_address = e.address"
                                to_cond = "t.to_address = e.address"
                            else:
                                from_cond = "LOWER(t.from_address) = LOWER(e.address)"
                                to_cond = "LOWER(t.to_address) = LOWER(e.address)"

                            cur.execute(f"""
                                UPDATE biz.onchain_transfer_log t
                                SET from_labels = e.label_types,
                                    from_label_names = e.label_names
                                FROM tmp_enrich_backfill e
                                WHERE t.chain = %s
                                  AND {from_cond}
                                  AND (t.from_labels IS NULL OR t.from_labels = ARRAY['unknown']::TEXT[])
                            """, (chain,))
                            from_up = cur.rowcount

                            cur.execute(f"""
                                UPDATE biz.onchain_transfer_log t
                                SET to_labels = e.label_types,
                                    to_label_names = e.label_names
                                FROM tmp_enrich_backfill e
                                WHERE t.chain = %s
                                  AND {to_cond}
                                  AND (t.to_labels IS NULL OR t.to_labels = ARRAY['unknown']::TEXT[])
                            """, (chain,))
                            to_up = cur.rowcount

                        conn.commit()
                        backfilled_total += from_up + to_up
                        batch_backfilled = from_up + to_up

                except Exception as e:
                    print(f"  ⚠️  本批写库失败: {e}")
                    import traceback
                    traceback.print_exc()

            # 累加到全局统计
            for k in batch_stats:
                stat_counts[k] += batch_stats[k]

            batch_end = min(batch_start + batch_size, len(addresses))
            pct = batch_end / len(addresses) * 100
            fail_parts = []
            if batch_stats["http_403"]:
                fail_parts.append(f"403×{batch_stats['http_403']}")
            if batch_stats["http_429"]:
                fail_parts.append(f"429×{batch_stats['http_429']}")
            if batch_stats["network_error"]:
                fail_parts.append(f"网络×{batch_stats['network_error']}")
            fail_str = f"，失败: {', '.join(fail_parts)}" if fail_parts else ""
            print(f"  ── 批次完成 {batch_end}/{len(addresses)} ({pct:.0f}%) ── "
                  f"本批查到标签 {batch_stats['ok']} 个, "
                  f"入库 {batch_inserted} 条, "
                  f"回填 {batch_backfilled} 条{fail_str}")

        # 清理所有线程的 fetcher
        for f in thread_local.values():
            f.close()

        elapsed = time.time() - t0
        print(f"\n  ── {chain} 完成 ──")
        print(f"    总耗时: {elapsed:.1f}s ({elapsed/60:.1f} 分钟)")
        print(f"    爬取地址: {len(addresses)} 个")
        print(f"    查到标签: {stat_counts['ok']} 个")
        print(f"    无标签: {stat_counts['no_label']} 个")
        print(f"    爬取失败: {sum(stat_counts[k] for k in ['http_403','http_429','http_other','network_error'])} 个")
        print(f"    入库新标签: {inserted_total} 条")
        print(f"    回填转账记录: {backfilled_total} 条")
        print(f"    平均速度: {len(addresses)/elapsed:.1f} 地址/秒")


def _thread_id() -> int:
    import threading
    return threading.get_ident()


if __name__ == "__main__":
    main()
