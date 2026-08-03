import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(storage_path) FROM biz.doc_asset")
        total, done = cur.fetchone()
        print(f"Downloaded: {done}/{total} ({done/total*100:.0f}%)" if total else "empty")
        cur.execute("SELECT sum(file_size_bytes) FROM biz.doc_asset WHERE storage_path IS NOT NULL")
        total_size = cur.fetchone()[0] or 0
        print(f"Total size: {total_size/1024/1024:.1f}MB")
