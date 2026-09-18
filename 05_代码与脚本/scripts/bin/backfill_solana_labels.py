"""Solana 地址标签全量回填脚本（基于 Vybe Network API）。

使用方式：
    python bin/backfill_solana_labels.py --api-key YOUR_VYBE_API_KEY
    python bin/backfill_solana_labels.py --api-key YOUR_KEY --dry-run
    python bin/backfill_solana_labels.py --api-key YOUR_KEY --proxy http://127.0.0.1:7890

工作原理：
1. 从 Vybe API 一次拉取所有已知标签（~10000 条）
2. 从数据库查出所有 Solana 转账涉及的无标签地址
3. 本地匹配，批量写入 onchain_address_label
4. 回填 onchain_transfer_log 的标签列

优势：只需要 1 次 API 调用（全量拉取），不需要逐个地址查，极快。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

sys.path.insert(0, 'src')

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


ENRICH_CONFIDENCE = "high"
ENRICH_SOURCE = "vybe_api"

# 高价值标签类型白名单（与 Explorer fetcher 对齐）
ALLOWED_LABEL_TYPES = {"exchange", "smart_money", "whale", "mev_bot", "market_maker", "dex"}


def get_unlabeled_addresses(conn, chain: str = "solana") -> list[str]:
    """查出所有无标签的 Solana 地址（按频次排序）。"""
    with conn.cursor() as cur:
        cur.execute("""
            WITH all_addrs AS (
                SELECT from_address AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND from_address IS NOT NULL
                UNION ALL
                SELECT to_address AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND to_address IS NOT NULL
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
            SELECT ac.addr
            FROM addr_counts ac
            LEFT JOIN labeled l ON ac.addr = l.addr
            WHERE l.addr IS NULL
            ORDER BY ac.cnt DESC
        """, (chain, chain, chain, chain))
        return [r[0] for r in cur.fetchall()]


def count_total_unlabeled(conn, chain: str = "solana") -> int:
    """统计无标签地址总数。"""
    with conn.cursor() as cur:
        cur.execute("""
            WITH all_addrs AS (
                SELECT from_address AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND from_address IS NOT NULL
                UNION
                SELECT to_address AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND to_address IS NOT NULL
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
            SELECT COUNT(*)
            FROM all_addrs aa
            LEFT JOIN labeled l ON aa.addr = l.addr
            WHERE l.addr IS NULL AND aa.addr IS NOT NULL AND aa.addr <> ''
        """, (chain, chain, chain, chain))
        return cur.fetchone()[0] or 0


def fetch_all_vybe_labels(api_key: str, proxy: str | None = None) -> dict[str, dict[str, Any]]:
    """从 Vybe API 拉取全部标签。

    返回 {address: label_info}，label_info 格式：
        {label_text, label_type, display_name, normalized_name, is_exchange, vybe_labels, entity_name}
    """
    from crypto_research.clients.vybe_label_fetcher import VybeLabelFetcher

    fetcher = VybeLabelFetcher(api_key=api_key, proxy=proxy)
    print(f"  从 Vybe API 拉取全量标签...")
    t0 = time.time()
    label_map = fetcher.fetch_all_labels()
    elapsed = time.time() - t0
    print(f"  ✅ 拉取完成：{len(label_map)} 个有标签地址，耗时 {elapsed:.1f}s")
    fetcher.close()
    return label_map


def match_and_write(conn, unlabeled_addrs: list[str],
                    vybe_label_map: dict[str, dict[str, Any]],
                    chain: str = "solana",
                    dry_run: bool = False) -> dict[str, int]:
    """本地匹配 Vybe 标签，写入数据库并回填转账记录。

    返回统计：{matched, inserted, backfilled}
    """
    # Solana 地址大小写敏感（Base58），直接精确匹配
    matched: dict[str, dict[str, Any]] = {}
    for addr in unlabeled_addrs:
        if addr in vybe_label_map:
            matched[addr] = vybe_label_map[addr]

    print(f"  匹配到 {len(matched)} 个有标签的地址")

    if not matched:
        return {"matched": 0, "inserted": 0, "backfilled": 0}

    if dry_run:
        # 打印前 10 个样例
        print(f"  [dry-run] 前 10 个样例：")
        for i, (addr, info) in enumerate(list(matched.items())[:10]):
            print(f"    {addr[:20]}... → {info['label_type']}: {info['display_name']}")
        return {"matched": len(matched), "inserted": 0, "backfilled": 0}

    # 1. 写入 onchain_address_label
    inserted = 0
    with conn.cursor() as cur:
        for addr, info in matched.items():
            if info["label_type"] not in ALLOWED_LABEL_TYPES:
                continue
            raw_meta = json.dumps({
                "entity_name": info.get("entity_name", ""),
                "vybe_labels": info.get("vybe_labels", []),
                "fetched_from": "vybe_api",
            }, ensure_ascii=False)
            cur.execute("""
                INSERT INTO biz.onchain_address_label
                    (address, chain, label_type, label_name, display_name,
                     confidence, source, raw_meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
            """, (
                addr, chain, info["label_type"],
                info["display_name"], info["display_name"],
                ENRICH_CONFIDENCE, ENRICH_SOURCE, raw_meta,
            ))
            if cur.rowcount:
                inserted += 1

        # 2. 交易所也写入 onchain_exchange_wallet（向后兼容）
        for addr, info in matched.items():
            if not info.get("is_exchange"):
                continue
            ex_name = info.get("normalized_name") or info["display_name"]
            cur.execute("""
                INSERT INTO biz.onchain_exchange_wallet
                    (address, exchange_name, chain, label, confidence, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (address, chain) DO UPDATE
                SET confidence = 'high',
                    exchange_name = EXCLUDED.exchange_name,
                    label = CASE
                        WHEN biz.onchain_exchange_wallet.label IS NULL
                             OR biz.onchain_exchange_wallet.label = ''
                        THEN EXCLUDED.label
                        ELSE biz.onchain_exchange_wallet.label
                    END,
                    source = COALESCE(NULLIF(biz.onchain_exchange_wallet.source, ''), '')
                               || ';vybe_api'
                WHERE biz.onchain_exchange_wallet.confidence != 'high'
            """, (
                addr, ex_name, chain,
                info["label_text"], ENRICH_CONFIDENCE, ENRICH_SOURCE,
            ))

    conn.commit()
    print(f"  ✅ 写入标签库：{inserted} 条新标签")

    # 3. 回填转账记录
    backfilled = 0
    if matched:
        matched_addrs = list(matched.keys())
        with conn.cursor() as cur:
            # 用临时表方式（与 label_enricher.py 同款）
            cur.execute("""
                CREATE TEMP TABLE tmp_vybe_labels (
                    address TEXT PRIMARY KEY,
                    label_types TEXT[],
                    label_names TEXT[],
                    is_exchange BOOLEAN DEFAULT false,
                    exchange_name TEXT
                ) ON COMMIT DROP
            """)

            rows = []
            for addr, info in matched.items():
                if info["label_type"] not in ALLOWED_LABEL_TYPES:
                    continue
                rows.append((
                    addr,
                    [info["label_type"]],
                    [info["display_name"]],
                    info.get("is_exchange", False),
                    info.get("normalized_name") if info.get("is_exchange") else None,
                ))

            if rows:
                cur.executemany("""
                    INSERT INTO tmp_vybe_labels (address, label_types, label_names, is_exchange, exchange_name)
                    VALUES (%s, %s, %s, %s, %s)
                """, rows)

                # 更新发件方
                cur.execute("""
                    UPDATE biz.onchain_transfer_log t
                    SET from_labels = e.label_types,
                        from_label_names = e.label_names
                    FROM tmp_vybe_labels e
                    WHERE t.chain = %s
                      AND t.from_address = e.address
                      AND (t.from_labels IS NULL OR t.from_labels = ARRAY['unknown']::TEXT[])
                """, (chain,))
                from_up = cur.rowcount

                # 更新收件方
                cur.execute("""
                    UPDATE biz.onchain_transfer_log t
                    SET to_labels = e.label_types,
                        to_label_names = e.label_names
                    FROM tmp_vybe_labels e
                    WHERE t.chain = %s
                      AND t.to_address = e.address
                      AND (t.to_labels IS NULL OR t.to_labels = ARRAY['unknown']::TEXT[])
                """, (chain,))
                to_up = cur.rowcount

                backfilled = from_up + to_up
                conn.commit()
                print(f"  ✅ 回填转账记录：{backfilled} 条（from: {from_up}, to: {to_up}）")

    return {"matched": len(matched), "inserted": inserted, "backfilled": backfilled}


def main():
    parser = argparse.ArgumentParser(description="Solana 地址标签全量回填（Vybe API）")
    parser.add_argument("--api-key", type=str, required=True, help="Vybe API key")
    parser.add_argument("--chain", type=str, default="solana")
    parser.add_argument("--proxy", type=str, default=None, help="代理地址")
    parser.add_argument("--dry-run", action="store_true", help="只看匹配数，不写库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    print("=" * 60)
    print(f"Solana 标签全量回填（Vybe API）")
    print("=" * 60)

    with get_connection(settings.database_url) as conn:
        # 1. 统计无标签地址
        total_unlabeled = count_total_unlabeled(conn, args.chain)
        print(f"\n  无标签地址总数：{total_unlabeled}")

        if total_unlabeled == 0:
            print("  没有无标签地址，退出。")
            return

        # 2. 从 Vybe 拉全量
        print()
        try:
            vybe_labels = fetch_all_vybe_labels(args.api_key, args.proxy)
        except Exception as e:
            print(f"  ❌ 拉取 Vybe 标签失败：{e}")
            sys.exit(1)

        if not vybe_labels:
            print("  ❌ Vybe 返回 0 条标签，请检查 API key")
            sys.exit(1)

        # 打印 Vybe 标签类型分布
        type_counts: dict[str, int] = {}
        for info in vybe_labels.values():
            t = info["label_type"]
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"  标签类型分布：{dict(sorted(type_counts.items(), key=lambda x: -x[1]))}")

        # 3. 获取无标签地址列表
        unlabeled = get_unlabeled_addresses(conn, args.chain)
        print(f"\n  待匹配地址：{len(unlabeled)} 个")

        # 4. 本地匹配 + 写库
        print()
        stats = match_and_write(conn, unlabeled, vybe_labels, args.chain, args.dry_run)

        print(f"\n{'=' * 60}")
        print(f"完成！")
        print(f"  Vybe 标签总数：{len(vybe_labels)}")
        print(f"  无标签地址数：{total_unlabeled}")
        print(f"  匹配成功：{stats['matched']}")
        if not args.dry_run:
            print(f"  新写入标签：{stats['inserted']}")
            print(f"  回填转账：{stats['backfilled']}")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
