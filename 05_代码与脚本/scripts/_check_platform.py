import sys
sys.path.insert(0, "src")
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM sys.source_platform WHERE platform_code='cmc'")
        print("CMC platform:", cur.fetchone())
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='sys' AND table_name='source_platform' ORDER BY ordinal_position")
        print("Columns:", [r[0] for r in cur.fetchall()])
