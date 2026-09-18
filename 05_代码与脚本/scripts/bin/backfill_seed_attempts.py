"""一次性回填：把前 N 个高频且无标签的地址标记为已尝试（no_label）。

用于修复第一次 backfill 没有记录尝试的历史债务。
用法:
    python backfill_seed_attempts.py --chain eth --limit 3000
"""
import sys
sys.path.insert(0, 'src')

import argparse
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
import psycopg.rows

CASE_SENSITIVE_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}
FETCH_ATTEMPT_TABLE = "biz.onchain_label_fetch_attempt"


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {FETCH_ATTEMPT_TABLE} (
                address TEXT NOT NULL,
                chain TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'explorer_html',
                status TEXT NOT NULL,
                attempt_count INT NOT NULL DEFAULT 1,
                last_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                PRIMARY KEY (address, chain, source)
            )
        """)
    conn.commit()


def seed_attempts(conn, chain: str, limit: int, source: str = "explorer_html") -> int:
    """把前 limit 个高频无标签地址标记为已尝试。"""
    case_sensitive = chain in CASE_SENSITIVE_CHAINS
    addr_from = "LOWER(from_address)" if not case_sensitive else "from_address"
    addr_to = "LOWER(to_address)" if not case_sensitive else "to_address"

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 先查出前 limit 个高频无标签地址
        cur.execute(f"""
            WITH all_addrs AS (
                SELECT {addr_from} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND from_address IS NOT NULL
                UNION ALL
                SELECT {addr_to} AS addr
                FROM biz.onchain_transfer_log
                WHERE chain = %s AND to_address IS NOT NULL
            ),
            addr_counts AS (
                SELECT addr, COUNT(*) AS cnt
                FROM all_addrs
                WHERE addr IS NOT NULL AND addr <> ''
                GROUP BY addr
                ORDER BY cnt DESC
                LIMIT %s
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
            SELECT ac.addr
            FROM addr_counts ac
            LEFT JOIN labeled l ON LOWER(ac.addr) = LOWER(l.addr)
            WHERE l.addr IS NULL
            ORDER BY ac.cnt DESC
        """, (chain, chain, limit, chain, chain))

        addrs = [r["addr"] for r in cur.fetchall()]
        print(f"  找到 {len(addrs)} 个前 {limit} 高频但无标签的地址")

        if not addrs:
            return 0

        # 批量插入（ON CONFLICT 跳过）
        cur.executemany(f"""
            INSERT INTO {FETCH_ATTEMPT_TABLE} (address, chain, source, status)
            VALUES (%s, %s, %s, 'no_label')
            ON CONFLICT (address, chain, source) DO NOTHING
        """, [(addr, chain, source) for addr in addrs])

        inserted = cur.rowcount
        conn.commit()
        return inserted


def main():
    parser = argparse.ArgumentParser(description="回填爬取尝试记录（修复历史债务）")
    parser.add_argument("--chain", type=str, default="eth")
    parser.add_argument("--limit", type=int, default=3000,
                        help="前 N 个高频无标签地址标记为已尝试")
    parser.add_argument("--source", type=str, default="explorer_html")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        if args.dry_run:
            print(f"[dry-run] 将标记 {args.chain} 链前 {args.limit} 个高频无标签地址为已尝试")
            return

        n = seed_attempts(conn, args.chain, args.limit, args.source)
        print(f"  ✅ 已标记 {n} 个地址为已尝试（no_label）")


if __name__ == "__main__":
    main()
