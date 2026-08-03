import sys

sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active) "
            "VALUES ('cg', 'CoinGecko', 'https://api.coingecko.com/api/v3', '去中心化加密数据聚合平台', true) "
            "ON CONFLICT (platform_code) DO NOTHING"
        )
    conn.commit()
print("done")
