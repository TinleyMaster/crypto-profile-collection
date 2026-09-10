"""历史转账记录标签回填：
用 biz.onchain_address_label + biz.onchain_exchange_wallet 重新给
biz.onchain_transfer_log 的 from_labels / to_labels / from_label_names / to_label_names 打标签。

分批处理，避免大事务锁表。默认每批 5000 条。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.address_label_resolver import AddressLabelResolver

settings = get_settings(require_database=True)

BATCH_SIZE = 5000


def backfill_chain(conn, chain: str, limit: int = 0):
    """回填单条链的标签。"""
    print(f"\n{'='*60}")
    print(f"回填 {chain} 链标签")
    print(f"{'='*60}")

    cur = conn.cursor()

    # 先看看总量
    cur.execute("""
        SELECT COUNT(*) FROM biz.onchain_transfer_log WHERE chain = %s
    """, (chain,))
    total = cur.fetchone()[0]
    print(f"  总记录数: {total}")

    if limit > 0:
        total = min(total, limit)
        print(f"  限制处理: {total} 条")

    resolver = AddressLabelResolver(conn, chain)

    offset = 0
    updated = 0
    processed = 0
    t0 = time.time()

    while offset < total:
        batch_limit = min(BATCH_SIZE, total - offset)

        # 取一批记录的地址
        cur.execute("""
            SELECT log_id, from_address, to_address
            FROM biz.onchain_transfer_log
            WHERE chain = %s
            ORDER BY log_id
            LIMIT %s OFFSET %s
        """, (chain, batch_limit, offset))
        rows = cur.fetchall()
        if not rows:
            break

        # 收集所有地址，批量解析
        all_addrs = set()
        for r in rows:
            all_addrs.add(r[1])  # from
            all_addrs.add(r[2])  # to
        resolver.resolve_batch(list(all_addrs))

        # 用临时表批量更新（比逐条 UPDATE 快几十倍）
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS tmp_label_backfill (
                log_id INTEGER PRIMARY KEY,
                from_labels TEXT[],
                to_labels TEXT[],
                from_label_names TEXT[],
                to_label_names TEXT[]
            ) ON COMMIT DROP
        """)
        cur.execute("TRUNCATE TABLE tmp_label_backfill")

        # 批量插入临时表
        temp_rows = []
        for log_id, from_addr, to_addr in rows:
            from_info = resolver.resolve(from_addr)
            to_info = resolver.resolve(to_addr)
            temp_rows.append((
                log_id,
                from_info["types"] or None,
                to_info["types"] or None,
                from_info["names"] or None,
                to_info["names"] or None,
            ))

        with cur.copy("COPY tmp_label_backfill (log_id, from_labels, to_labels, from_label_names, to_label_names) FROM STDIN") as copy:
            for row in temp_rows:
                copy.write_row(row)

        # 一条 UPDATE 批量更新主表
        cur.execute("""
            UPDATE biz.onchain_transfer_log t
            SET from_labels = tmp.from_labels,
                to_labels = tmp.to_labels,
                from_label_names = tmp.from_label_names,
                to_label_names = tmp.to_label_names
            FROM tmp_label_backfill tmp
            WHERE t.log_id = tmp.log_id
              AND (t.from_labels IS DISTINCT FROM tmp.from_labels
                OR t.to_labels IS DISTINCT FROM tmp.to_labels
                OR t.from_label_names IS DISTINCT FROM tmp.from_label_names
                OR t.to_label_names IS DISTINCT FROM tmp.to_label_names)
        """)
        batch_updated = cur.rowcount
        conn.commit()

        updated += batch_updated
        processed += len(rows)
        offset += len(rows)

        elapsed = time.time() - t0
        rate = processed / elapsed if elapsed > 0 else 0
        print(f"  进度: {processed}/{total} ({processed/total*100:.1f}%) "
              f"| 本批更新: {batch_updated} | 累计更新: {updated} "
              f"| 速度: {rate:.0f} 条/s")

    elapsed = time.time() - t0
    print(f"\n  ✅ {chain} 完成：更新 {updated} 条，耗时 {elapsed:.1f}s")
    return updated


def main():
    import argparse
    parser = argparse.ArgumentParser(description="回填历史转账记录的标签数组列")
    parser.add_argument("--chain", type=str, default="", help="指定链，不填则全部链")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条数（调试用）")
    args = parser.parse_args()

    all_chains = ["eth", "bsc", "polygon", "arbitrum", "base", "optimism",
                  "avalanche", "solana", "tron", "ton", "sui", "aptos"]

    chains = [args.chain] if args.chain else all_chains

    with get_connection(settings.database_url) as conn:
        total_updated = 0
        for chain in chains:
            # 先检查该链有没有记录
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM biz.onchain_transfer_log WHERE chain = %s
            """, (chain,))
            cnt = cur.fetchone()[0]
            if cnt == 0:
                print(f"\n  ⏭  {chain}: 无记录，跳过")
                continue
            total_updated += backfill_chain(conn, chain, args.limit)

        print(f"\n{'='*60}")
        print(f"全部完成，累计更新 {total_updated} 条")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
