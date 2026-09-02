import sys

sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)

with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        # 找到孤儿 ID
        cur.execute("""
            SELECT a.asset_id FROM core.asset a
            LEFT JOIN core.asset_source_map asm ON asm.asset_id = a.asset_id
            WHERE asm.source_asset_key IS NULL
        """)
        orphan_ids = [r[0] for r in cur.fetchall()]
        print(f"orphan count: {len(orphan_ids)}")

        if orphan_ids:
            # 逐批删除
            cur.execute(
                "DELETE FROM core.asset_source_map WHERE asset_id = ANY(%s)",
                (orphan_ids,),
            )
            print(f"source_map deleted: {cur.rowcount}")
            cur.execute(
                "DELETE FROM core.asset WHERE asset_id = ANY(%s)", (orphan_ids,)
            )
            print(f"asset deleted: {cur.rowcount}")
            conn.commit()

        cur.execute("SELECT COUNT(*) FROM core.asset")
        print(f"core.asset remaining: {cur.fetchone()[0]}")
