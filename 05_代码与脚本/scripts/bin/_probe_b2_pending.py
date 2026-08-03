import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)
with get_connection(settings.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_type, count(*) FROM biz.doc_source_entry "
            "WHERE entry_type = ANY(%s) AND deep_crawled_at IS NULL "
            "GROUP BY entry_type ORDER BY entry_type",
            (["docs", "official_website"],),
        )
        for row in cur.fetchall():
            print(f"{row[0]}: {row[1]}")
        cur.execute(
            "SELECT count(*) FROM biz.doc_source_entry "
            "WHERE entry_type = ANY(%s) AND deep_crawled_at IS NULL",
            (["docs", "official_website"],),
        )
        total = cur.fetchone()[0]
        print(f"Total pending: {total}")
