import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

CRAWLABLE_TYPES = [
    "official_website", "docs",
]

parser = argparse.ArgumentParser(description="查询 B2 待处理条目数")
parser.add_argument("--min-asset-id", type=int, default=0, help="仅统计 asset_id >= 该值的资产（新入库币），0 表示不过滤")
args = parser.parse_args()

_min_asset_clause = ""
_min_asset_params: list = []
if args.min_asset_id and args.min_asset_id > 0:
    _min_asset_clause = " AND asset_id >= %s"
    _min_asset_params = [args.min_asset_id]

settings = get_settings(require_database=True)
with get_connection(settings.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_type, count(*) FROM biz.doc_source_entry "
            "WHERE entry_type = ANY(%s) AND deep_crawled_at IS NULL "
            "AND discovered_from NOT LIKE 'deep_crawl:%%'"
            + _min_asset_clause
            + " GROUP BY entry_type ORDER BY entry_type",
            tuple([CRAWLABLE_TYPES] + _min_asset_params),
        )
        for row in cur.fetchall():
            print(f"{row[0]}: {row[1]}")
        cur.execute(
            "SELECT count(*) FROM biz.doc_source_entry "
            "WHERE entry_type = ANY(%s) AND deep_crawled_at IS NULL "
            "AND discovered_from NOT LIKE 'deep_crawl:%%'"
            + _min_asset_clause,
            tuple([CRAWLABLE_TYPES] + _min_asset_params),
        )
        total = cur.fetchone()[0]
        print(f"Total pending: {total}")
