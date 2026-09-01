import json, sys
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
queries = [
    ("cmc_info", "select count(*) from src_cmc.cmc_asset_info"),
    ("core_asset", "select count(*) from core.asset"),
    ("doc_source_entry", "select count(*) from biz.doc_source_entry"),
    ("doc_asset", "select count(*) from biz.doc_asset"),
]
out = {}
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        for k, sql in queries:
            cur.execute(sql)
            out[k] = cur.fetchone()[0]
print(json.dumps(out, ensure_ascii=False))
