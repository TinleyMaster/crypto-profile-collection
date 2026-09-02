import sys
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.db.upsert import load_sql

s = get_settings(require_database=True)

# Apply schema
schema_sql = load_sql("src_dl/schema.sql")
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.commit()
print("schema applied")

# Register source_platform
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active) "
            "VALUES ('dl', 'DefiLlama', 'https://api.llama.fi', 'DeFi TVL数据聚合平台', true) "
            "ON CONFLICT (platform_code) DO NOTHING"
        )
    conn.commit()
print("platform registered")

# Register endpoints
endpoints = [
    ("dl_protocols", "dl", "GET", "/protocols", "protocol_list", "daily", False, "All DeFi protocols with TVL"),
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
print("endpoints registered")
print("all done")
