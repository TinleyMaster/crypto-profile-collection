import sys
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
endpoints = [
    ("coin_list", "cg", "GET", "/coins/list", "coin_list", "weekly", False, "All supported coins list"),
    ("coin_info", "cg", "GET", "/coins/{id}", "coin_detail", "daily", False, "Single coin detail with market data"),
]

with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        for ep in endpoints:
            cur.execute(
                "INSERT INTO sys.source_endpoint (endpoint_code, platform_code, http_method, endpoint_path, entity_type, update_granularity, is_deprecated, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (endpoint_code) DO NOTHING",
                ep
            )
    conn.commit()
print("done")
