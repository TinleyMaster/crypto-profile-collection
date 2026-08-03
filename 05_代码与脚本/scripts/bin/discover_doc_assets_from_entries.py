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
        description="Discover direct-file doc assets from biz.doc_source_entry."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview discovered doc assets without writing database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of source entries to scan.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=8,
        help="Per-entry probe timeout in seconds.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    import psycopg

    from crypto_research.clients.http_client import SimpleHttpClient
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import fetch_one, load_sql
    from crypto_research.mapping.doc_asset_discovery import (
        extract_file_name,
        infer_doc_type,
        looks_like_pdf_url,
        should_probe_direct_asset,
    )

    settings = get_settings(require_database=True)
    http = SimpleHttpClient(timeout_seconds=args.timeout_seconds)

    select_entries_sql = load_sql("biz/select_doc_source_entries_for_discovery.sql")
    upsert_doc_asset_sql = load_sql("biz/upsert_doc_asset.sql")

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(select_entries_sql, (args.limit,))
            entry_rows = [dict(row) for row in cur.fetchall()]

    discovered_rows: list[dict[str, object]] = []
    skipped_entries = 0
    failed_probes = 0
    for entry in entry_rows:
        entry_url = entry["entry_url"]
        if looks_like_pdf_url(entry_url):
            probe = {
                "ok": True,
                "status_code": None,
                "final_url": entry_url,
                "content_type": "application/pdf",
                "content_length": None,
                "method": "SKIP_PROBE",
            }
        else:
            if not should_probe_direct_asset(entry["entry_type"], entry_url):
                skipped_entries += 1
                continue
            probe = http.probe(entry_url)

        if not probe.get("ok"):
            failed_probes += 1
            continue
        final_url = probe.get("final_url") or entry_url
        content_type = (probe.get("content_type") or "").lower()

        is_pdf = looks_like_pdf_url(final_url) or "application/pdf" in content_type
        if not is_pdf:
            continue

        content_length = probe.get("content_length")
        file_size_bytes = (
            int(content_length)
            if content_length and str(content_length).isdigit()
            else None
        )

        discovered_rows.append(
            {
                "entity_type": entry["entity_type"],
                "asset_id": entry["asset_id"],
                "protocol_id": entry["protocol_id"],
                "entry_id": entry["entry_id"],
                "doc_type": infer_doc_type(entry["entry_type"], final_url),
                "source_url": entry_url,
                "resolved_url": final_url,
                "file_name": extract_file_name(final_url),
                "mime_type": probe.get("content_type"),
                "file_size_bytes": file_size_bytes,
                "parse_status": "待解析",
                "sync_status": "待同步",
            }
        )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "scanned_entries": len(entry_rows),
                    "skipped_entries": skipped_entries,
                    "failed_probes": failed_probes,
                    "discovered_count": len(discovered_rows),
                    "first_discovered": discovered_rows[0]
                    if discovered_rows
                    else None,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    written = 0
    with get_connection(settings.database_url) as conn:
        for row in discovered_rows:
            fetch_one(
                conn,
                upsert_doc_asset_sql,
                (
                    row["entity_type"],
                    row["asset_id"],
                    row["protocol_id"],
                    row["entry_id"],
                    row["doc_type"],
                    row["source_url"],
                    row["resolved_url"],
                    row["file_name"],
                    row["mime_type"],
                    row["file_size_bytes"],
                    row["parse_status"],
                    row["sync_status"],
                ),
            )
            written += 1

    print(
        json.dumps(
            {
                "status": "success",
                "scanned_entries": len(entry_rows),
                "skipped_entries": skipped_entries,
                "failed_probes": failed_probes,
                "discovered_count": len(discovered_rows),
                "written_rows": written,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
