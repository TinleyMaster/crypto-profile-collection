import sys
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.db.upsert import load_sql

s = get_settings(require_database=True)
sql = load_sql("src_cg/select_missing_coin_info_ids.sql")
print("SQL:", repr(sql[:200]))
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(sql, (10,))
        row = cur.fetchone()
        result = row[0] if row else None
        print(f"Result: {result}")
        if result:
            print(f"Count: {len(result)}, First: {result[:3]}")
