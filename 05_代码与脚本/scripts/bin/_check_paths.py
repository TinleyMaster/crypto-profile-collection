import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        # Check path format
        cur.execute("""
            SELECT 
                CASE 
                    WHEN storage_path LIKE '%\_%\/whitepapers\/%' ESCAPE '\' THEN 'new'
                    WHEN storage_path LIKE '%\\asset\\%' OR storage_path LIKE '%/asset/%' THEN 'old'
                    ELSE 'other'
                END AS fmt,
                count(*)
            FROM biz.doc_asset WHERE storage_path IS NOT NULL 
            GROUP BY 1
        """)
        for fmt, cnt in cur.fetchall():
            print(f"  {fmt}: {cnt}")

        # Show some paths
        cur.execute("SELECT storage_path FROM biz.doc_asset WHERE storage_path IS NOT NULL LIMIT 3")
        for r in cur.fetchall():
            print(f"  {r[0]}")
