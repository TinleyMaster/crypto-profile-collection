"""探测 doc_asset 当前状态"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)

with get_connection(settings.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*), count(storage_path), count(content_hash)
            FROM biz.doc_asset
        """)
        total, has_path, has_hash = cur.fetchone()
        print(f"doc_asset total: {total}, has_path: {has_path}, has_hash: {has_hash}")

        cur.execute("""
            SELECT mime_type, count(*) FROM biz.doc_asset
            GROUP BY mime_type ORDER BY count(*) DESC
        """)
        print("MIME types:")
        for mime, cnt in cur.fetchall():
            print(f"  {mime}: {cnt}")

        cur.execute("""
            SELECT doc_id, source_url, file_name, mime_type, storage_path
            FROM biz.doc_asset
            WHERE storage_path IS NULL
            ORDER BY doc_id
            LIMIT 5
        """)
        print("Samples to download:")
        for row in cur.fetchall():
            print(f"  {row}")
