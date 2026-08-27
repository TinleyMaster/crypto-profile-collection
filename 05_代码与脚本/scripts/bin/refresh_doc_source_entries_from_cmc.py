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
        description="Refresh biz.doc_source_entry from mapped CMC info URLs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview entries without writing database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of mapped assets to scan.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql
    from crypto_research.mapping.doc_source_entries import extract_doc_source_entries

    settings = get_settings(require_database=True)
    select_candidates_sql = load_sql("src_cmc/select_cmc_doc_source_candidates.sql")
    upsert_entry_sql = load_sql("biz/upsert_doc_source_entry.sql")

    result: dict[str, object]

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_candidates_sql, (args.limit,))
            source_rows = [dict(row) for row in cur.fetchall()]

        entries: list[dict[str, object]] = []
        for row in source_rows:
            urls = row.get("urls") or {}
            entries.extend(
                extract_doc_source_entries(
                    asset_id=row["asset_id"],
                    cmc_id=row["cmc_id"],
                    urls=urls,
                )
            )

        if args.dry_run:
            result = {
                "mode": "dry-run",
                "asset_count": len(source_rows),
                "entry_count": len(entries),
                "first_entry": entries[0] if entries else None,
            }
        else:
            written = 0
            for entry in entries:
                fetch_one(
                    conn,
                    upsert_entry_sql,
                    (
                        entry["entity_type"],
                        entry["asset_id"],
                        entry["protocol_id"],
                        entry["source_code"],
                        entry["entry_type"],
                        entry["entry_url"],
                        entry["discovered_from"],
                        entry["is_primary"],
                        entry["content_topics"],
                        entry["classify_method"],
                        entry["classify_confidence"],
                    ),
                )
                written += 1

            result = {
                "status": "success",
                "asset_count": len(source_rows),
                "entry_count": len(entries),
                "written_rows": written,
            }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
