from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC cryptocurrency map into sys/raw/src_cmc tables."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    parser.add_argument(
        "--listing-status", default="active", help="CMC listing_status parameter."
    )
    parser.add_argument("--sort", default="cmc_rank", help="CMC sort parameter.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    from crypto_research.clients.cmc_client import CMCClient
    from crypto_research.config import get_settings
    from crypto_research.db.upsert import load_sql
    from crypto_research.parsers.cmc_map import parse_cmc_map_payload
    from crypto_research.utils.hash_utils import md5_text
    from crypto_research.utils.json_utils import stable_json_dumps

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    payload = client.get_cryptocurrency_map(
        listing_status=args.listing_status,
        sort=args.sort,
    )
    payload_text = stable_json_dumps(payload)
    payload_hash = md5_text(payload_text)
    fetched_at = datetime.now(timezone.utc).isoformat()

    if args.dry_run:
        parsed_rows = parse_cmc_map_payload(payload, raw_response_id=None)
        preview = {
            "mode": "dry-run",
            "row_count": len(parsed_rows),
            "payload_hash": payload_hash,
            "first_row": parsed_rows[0] if parsed_rows else None,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    insert_ingest_run_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_run_sql = load_sql("sys/finish_ingest_run.sql")
    insert_raw_sql = load_sql("raw/insert_api_response.sql")
    upsert_map_sql = load_sql("src_cmc/upsert_cmc_asset_map.sql")

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, fetch_one, load_sql

    with get_connection(settings.database_url) as conn:
        run_row = fetch_one(
            conn,
            insert_ingest_run_sql,
            (
                "cmc",
                "cmc_map",
                "WF_CMC_MAP_INGEST",
                json.dumps(
                    {
                        "listing_status": args.listing_status,
                        "sort": args.sort,
                    },
                    ensure_ascii=False,
                ),
                f"{settings.cmc_base_url}/v1/cryptocurrency/map",
            ),
        )
        run_id = run_row["run_id"]

        try:
            raw_row = fetch_one(
                conn,
                insert_raw_sql,
                (
                    run_id,
                    "cmc",
                    "cmc_map",
                    f"listing_status={args.listing_status}&sort={args.sort}",
                    None,
                    "page:all",
                    payload_text,
                    payload_hash,
                    fetched_at,
                ),
            )
            raw_response_id = raw_row["response_id"]

            parsed_rows = parse_cmc_map_payload(
                payload, raw_response_id=raw_response_id
            )
            row_params = [
                (
                    row["cmc_id"],
                    row["symbol"],
                    row["name"],
                    row["slug"],
                    row["listing_status"],
                    row["is_active"],
                    row["rank_num"],
                    row["platform_name"],
                    row["platform_slug"],
                    row["platform_symbol"],
                    row["token_address"],
                    row["first_historical_data"],
                    row["last_historical_data"],
                    row["raw_response_id"],
                    row["fetched_at"],
                )
                for row in parsed_rows
            ]
            execute_many(conn, upsert_map_sql, row_params)

            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success",
                    200,
                    len(parsed_rows),
                    len(parsed_rows),
                    0,
                    None,
                    run_id,
                ),
            )

            print(
                json.dumps(
                    {
                        "status": "success",
                        "run_id": run_id,
                        "raw_response_id": raw_response_id,
                        "row_count": len(parsed_rows),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except Exception as exc:
            conn.rollback()
            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "failed",
                    None,
                    None,
                    None,
                    None,
                    str(exc),
                    run_id,
                ),
            )
            raise


if __name__ == "__main__":
    raise SystemExit(main())
