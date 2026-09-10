"""
修复 biz.onchain_exchange_wallet 中 BSC 链的大小写重复地址。

背景：历史导入时对同一地址存了大小写两个版本（EVM 地址应该统一小写）。
策略：保留小写版本，删除大写版本（同 address+chain 唯一约束下，小写是规范形式）。

用法：
  python fix_bsc_dup_addresses.py           # 预览
  python fix_bsc_dup_addresses.py --apply   # 执行删除
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


def find_duplicates(conn) -> list[dict]:
    """找出 BSC 链上同一地址（忽略大小写）有重复记录的条目。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT LOWER(address) AS addr_norm,
                   COUNT(*) AS cnt,
                   ARRAY_AGG(wallet_id ORDER BY wallet_id) AS ids,
                   ARRAY_AGG(address ORDER BY wallet_id) AS addresses,
                   ARRAY_AGG(confidence ORDER BY wallet_id) AS confidences,
                   ARRAY_AGG(exchange_name ORDER BY wallet_id) AS exchanges
            FROM biz.onchain_exchange_wallet
            WHERE chain = 'bsc'
            GROUP BY LOWER(address)
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC, addr_norm
        """)
        return [dict(r) for r in cur.fetchall()]


def fix_duplicates(conn, dups: list[dict]) -> int:
    """删除每组重复中的大写版本，保留小写版本。返回删除条数。"""
    deleted = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for dup in dups:
            addr_norm = dup["addr_norm"]
            # 找出所有记录
            cur.execute("""
                SELECT wallet_id, address, confidence, exchange_name
                FROM biz.onchain_exchange_wallet
                WHERE chain = 'bsc' AND LOWER(address) = %s
                ORDER BY wallet_id
            """, (addr_norm,))
            rows = [dict(r) for r in cur.fetchall()]

            # 策略：保留地址全小写的那一条；如果都不标准，保留 wallet_id 最小的
            keep_id = None
            delete_ids = []
            for r in rows:
                if r["address"] == addr_norm and keep_id is None:
                    keep_id = r["wallet_id"]
                else:
                    delete_ids.append(r["wallet_id"])

            # 如果都没匹配到小写，保留第一条
            if keep_id is None and rows:
                keep_id = rows[0]["wallet_id"]
                delete_ids = [r["wallet_id"] for r in rows[1:]]

            for did in delete_ids:
                cur.execute("""
                    DELETE FROM biz.onchain_exchange_wallet
                    WHERE wallet_id = %s
                """, (did,))
                if cur.rowcount:
                    deleted += 1
                    print(f"  [DELETE] wallet_id={did} addr={[r['address'] for r in rows if r['wallet_id']==did][0]} "
                          f"→ 保留 wallet_id={keep_id}")

    conn.commit()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="修复 BSC 链交易所地址的大小写重复")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认预览）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        dups = find_duplicates(conn)

        if not dups:
            print("✅ BSC 链无大小写重复地址")
            return 0

        print(f"发现 {len(dups)} 组重复地址：\n")
        for dup in dups:
            print(f"  {dup['addr_norm']}: {dup['cnt']} 条")
            for i, (aid, addr, conf, exch) in enumerate(zip(
                    dup["ids"], dup["addresses"], dup["confidences"], dup["exchanges"])):
                marker = " ← 保留（小写）" if addr == dup["addr_norm"] else "   删除"
                print(f"    #{aid}  {addr}  {exch}  ({conf}){marker}")

        if not args.apply:
            print(f"\n[DRY-RUN] 将删除 {sum(d['cnt'] - 1 for d in dups)} 条重复记录。加 --apply 执行。")
            return 0

        print(f"\n执行删除...")
        deleted = fix_duplicates(conn, dups)
        print(f"\n✅ 删除完成，共删除 {deleted} 条重复记录")

        # 验证
        dups_after = find_duplicates(conn)
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT COUNT(*) AS total FROM biz.onchain_exchange_wallet WHERE chain = 'bsc'")
            total = cur.fetchone()["total"]
        print(f"验证：剩余 {total} 条 BSC 地址，重复 {len(dups_after)} 组")

    return 0


if __name__ == "__main__":
    sys.exit(main())
