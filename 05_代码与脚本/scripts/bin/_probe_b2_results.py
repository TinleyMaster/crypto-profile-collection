import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)
with get_connection(settings.database_url) as conn:
    with conn.cursor() as cur:
        # deep crawl entries
        cur.execute(
            "SELECT count(*) FROM biz.doc_source_entry WHERE discovered_from LIKE %s",
            ("deep_crawl:%",),
        )
        print(f"deep_crawl entries: {cur.fetchone()[0]}")

        cur.execute(
            "SELECT entry_type, count(*) FROM biz.doc_source_entry "
            "WHERE discovered_from LIKE %s "
            "GROUP BY entry_type ORDER BY count(*) DESC",
            ("deep_crawl:%",),
        )
        for row in cur.fetchall():
            print(f"  {row[0]}: {row[1]}")

        cur.execute("SELECT count(*) FROM biz.doc_source_entry")
        print(f"Total doc_source_entry: {cur.fetchone()[0]}")

        # show some sample links
        cur.execute(
            "SELECT entry_url, entry_type, discovered_from FROM biz.doc_source_entry "
            "WHERE discovered_from LIKE %s "
            "ORDER BY entry_id DESC LIMIT 10",
            ("deep_crawl:%",),
        )
        print("\nRecent discovered links:")
        for row in cur.fetchall():
            print(f"  [{row[1]}] {row[0][:100]}")
            print(f"       from: {row[2][:80]}")
