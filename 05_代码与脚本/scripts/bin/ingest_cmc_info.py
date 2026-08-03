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
        description="Ingest CMC cryptocurrency info into sys/raw/src_cmc tables."
    )
    parser.add_argument(
        "--ids",
        help="Comma separated CMC IDs, for example: 1,1027,825",
    )
    parser.add_argument(
        "--from-map-missing",
        action="store_true",
        help="Load missing CMC IDs from src_cmc.cmc_asset_map minus src_cmc.cmc_asset_info.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Batch size when using --from-map-missing. Default: 100",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    return parser


def parse_ids(raw_ids: str) -> list[int]:
    values: list[int] = []
    for item in raw_ids.split(","):
        text = item.strip()
        if not text:
            continue
        values.append(int(text))
    if not values:
        raise ValueError("At least one CMC id is required")
    return values


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.cmc_client import CMCClient
    from crypto_research.config import get_settings
    from crypto_research.db.upsert import load_sql
    from crypto_research.parsers.cmc_info import parse_cmc_info_payload
    from crypto_research.utils.hash_utils import md5_text
    from crypto_research.utils.json_utils import stable_json_dumps

    settings = get_settings(require_database=not args.dry_run)

    ids: list[int]
    if args.ids:
        ids = parse_ids(args.ids)
    elif args.from_map_missing:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is required when using --from-map-missing")
        from crypto_research.db.conn import get_connection
        from crypto_research.db.upsert import fetch_one

        select_missing_ids_sql = load_sql("src_cmc/select_missing_cmc_info_ids.sql")
        with get_connection(settings.database_url) as conn:
            result = fetch_one(conn, select_missing_ids_sql, (args.limit,))
        ids = result.get("cmc_ids") or []
        if not ids:
            print(
                json.dumps(
                    {
                        "status": "noop",
                        "message": "No missing CMC info ids found.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    else:
        raise ValueError("Either --ids or --from-map-missing must be provided")

    client = CMCClient(settings)

    payload = client.get_cryptocurrency_info(ids)
    payload_text = stable_json_dumps(payload)
    payload_hash = md5_text(payload_text)
    fetched_at = datetime.now(timezone.utc).isoformat()

    if args.dry_run:
        parsed_rows = parse_cmc_info_payload(payload, raw_response_id=None)
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "ids": ids,
                    "row_count": len(parsed_rows),
                    "payload_hash": payload_hash,
                    "first_row": parsed_rows[0] if parsed_rows else None,
                    "fetched_at": fetched_at,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    load_sql("sys/insert_ingest_run.sql")
    insert_ingest_run_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_run_sql = load_sql("sys/finish_ingest_run.sql")
    insert_raw_sql = load_sql("raw/insert_api_response.sql")
    upsert_info_sql = load_sql("src_cmc/upsert_cmc_asset_info.sql")

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, fetch_one

    with get_connection(settings.database_url) as conn:
        run_row = fetch_one(
            conn,
            insert_ingest_run_sql,
            (
                "cmc",
                "cmc_info",
                "WF_CMC_INFO_BATCH",
                json.dumps(
                    {
                        "ids": ids,
                    },
                    ensure_ascii=False,
                ),
                f"{settings.cmc_base_url}/v2/cryptocurrency/info",
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
                    "cmc_info",
                    "id=" + ",".join(str(value) for value in ids),
                    None,
                    "page:single",
                    payload_text,
                    payload_hash,
                    fetched_at,
                ),
            )
            raw_response_id = raw_row["response_id"]

            parsed_rows = parse_cmc_info_payload(
                payload, raw_response_id=raw_response_id
            )
            row_params = [
                (
                    row["cmc_id"],
                    row["description"],
                    row["logo"],
                    row["notice"],
                    row["date_launched"],
                    json.dumps(row["tags"], ensure_ascii=False),
                    json.dumps(row["urls"], ensure_ascii=False),
                    json.dumps(row["platform_json"], ensure_ascii=False),
                    row["category_hint"],
                    row["raw_response_id"],
                    row["fetched_at"],
                )
                for row in parsed_rows
            ]
            execute_many(conn, upsert_info_sql, row_params)

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
                        "ids": ids,
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
