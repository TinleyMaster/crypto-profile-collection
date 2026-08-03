import sys

sys.path.insert(0, "src")
from crypto_research.db.conn import get_connection
from crypto_research.db.upsert import load_sql
from crypto_research.config import get_settings

s = get_settings()
sql = load_sql("src_cg/schema.sql")

with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
print("schema ok")
