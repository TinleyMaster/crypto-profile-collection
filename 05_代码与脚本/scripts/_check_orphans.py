import sys
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)

with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        # 1. 非 CMC 的 source_map 有哪些
        cur.execute("""
            SELECT asm.source_code, COUNT(*) as cnt
            FROM core.asset_source_map asm
            WHERE asm.source_code != 'cmc'
            GROUP BY asm.source_code
            ORDER BY cnt DESC
        """)
        rows = cur.fetchall()
        print("=== 非CMC source_map ===")
        for r in rows:
            print(f"  {r[0]}: {r[1]}")

        # 2. 看 core.asset 表结构
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='core' AND table_name='asset'
            ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
        print(f"\n=== core.asset 列: {cols} ===")

        # 3. 没有任何 source_map 的资产
        cur.execute("""
            SELECT COUNT(*) FROM core.asset a
            LEFT JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id
            WHERE asm.source_asset_key IS NULL
        """)
        print(f"\n=== 无 source_map 的资产: {cur.fetchone()[0]} ===")

        # 4. 取前 10 条样例
        cur.execute("""
            SELECT a.asset_id, a.asset_symbol, a.created_at
            FROM core.asset a
            LEFT JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id
            WHERE asm.source_asset_key IS NULL
            ORDER BY a.created_at
            LIMIT 10
        """)
        print("\n=== 样例 (无 source_map 的资产) ===")
        for r in cur.fetchall():
            print(f"  id={r[0]} symbol={r[1]} created={r[2]}")
