"""清理 doc_asset 的 storage_path，准备重新下载"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
with get_connection(s.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE biz.doc_asset
            SET storage_path = NULL, content_hash = NULL, file_size_bytes = NULL
            WHERE storage_path IS NOT NULL
        """)
        print(f"Cleared {cur.rowcount} storage paths")
