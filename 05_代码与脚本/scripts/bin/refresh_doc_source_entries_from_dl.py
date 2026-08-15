from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh biz.doc_source_entry from mapped DefiLlama protocol URLs."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    parser.add_argument("--limit", type=int, default=100, help="Max assets to scan.")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql
    from crypto_research.mapping.classify_link import classify_entry_fields

    settings = get_settings(require_database=True)
    select_candidates_sql = load_sql("src_dl/select_dl_doc_source_candidates.sql")
    upsert_entry_sql = load_sql("biz/upsert_doc_source_entry.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_candidates_sql, (args.limit,))
            source_rows = [dict(row) for row in cur.fetchall()]

        entries: list[dict] = []
        for row in source_rows:
            # Official website
            url = row.get("url")
            if url and isinstance(url, str) and url.strip():
                topics, method, confidence = classify_entry_fields(url.strip(), source_code="dl")
                entries.append({
                    "entity_type": "asset",
                    "asset_id": row["asset_id"],
                    "protocol_id": None,
                    "source_code": "dl",
                    "entry_type": "official_website",
                    "entry_url": url.strip(),
                    "discovered_from": "dl_info.url",
                    "is_primary": True,
                    "content_topics": topics,
                    "classify_method": method,
                    "classify_confidence": confidence,
                })
            # Twitter
            twitter = row.get("twitter")
            if twitter and isinstance(twitter, str) and twitter.strip():
                twitter_url = f"https://twitter.com/{twitter.strip()}" if not twitter.startswith("http") else twitter.strip()
                topics, method, confidence = classify_entry_fields(twitter_url, source_code="dl")
                entries.append({
                    "entity_type": "asset",
                    "asset_id": row["asset_id"],
                    "protocol_id": None,
                    "source_code": "dl",
                    "entry_type": "other",
                    "entry_url": twitter_url,
                    "discovered_from": "dl_info.twitter",
                    "is_primary": False,
                    "content_topics": topics,
                    "classify_method": method,
                    "classify_confidence": confidence,
                })

        if args.dry_run:
            result = {"mode": "dry-run", "asset_count": len(source_rows), "entry_count": len(entries),
                      "first_entry": entries[0] if entries else None}
        else:
            written = 0
            for entry in entries:
                fetch_one(conn, upsert_entry_sql,
                    (entry["entity_type"], entry["asset_id"], entry["protocol_id"],
                     entry["source_code"], entry["entry_type"], entry["entry_url"],
                     entry["discovered_from"], entry["is_primary"],
                     entry["content_topics"], entry["classify_method"], entry["classify_confidence"]))
                written += 1
            result = {"status": "success", "asset_count": len(source_rows),
                      "entry_count": len(entries), "written_rows": written}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
