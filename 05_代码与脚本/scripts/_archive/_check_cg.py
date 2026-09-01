import json, sys

sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
queries = [
    ("cg_coin_list", "select count(*) from src_cg.coin_list"),
    ("cg_coin_info", "select count(*) from src_cg.coin_info"),
]
out = {}
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        for k, sql in queries:
            cur.execute(sql)
            out[k] = cur.fetchone()[0]
with open("_cg_result.txt", "w") as f:
    f.write(json.dumps(out, ensure_ascii=False))
print("ok")
