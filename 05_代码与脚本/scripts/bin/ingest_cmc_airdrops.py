"""Ingest CMC airdrops into biz.airdrop_event.

Populates:
  - biz.airdrop_event (from /v1/cryptocurrency/airdrops)

Usage:
    python ingest_cmc_airdrops.py
    python ingest_cmc_airdrops.py --dry-run
    python ingest_cmc_airdrops.py --status UPCOMING
"""
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
        description="Ingest CMC airdrops into biz.airdrop_event."
    )
    parser.add_argument(
        "--status",
        default="ONGOING",
        choices=["ENDED", "ONGOING", "UPCOMING"],
        help="Airdrop status to fetch. Default: ONGOING",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max airdrops to fetch. Default: 500",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.cmc_client import CMCClient
    from crypto_research.config import get_settings
    from crypto_research.db.upsert import load_sql
    from crypto_research.parsers.cmc_analysis import parse_airdrop_payload

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    all_rows: list[dict] = []
    fetched_raw: list[tuple[str, str]] = []  # (page_key, raw_text)
    page_size = 100

    start = 1
    while len(all_rows) < args.limit:
        limit = min(page_size, args.limit - len(all_rows))
        try:
            payload = client.get_airdrops(start=start, limit=limit, status=args.status)
        except Exception as e:
            print(f"[airdrop] 分页 {start} 拉取失败: {e}", file=sys.stderr)
            break

        fetched_raw.append(
            (f"start={start},limit={limit}", json.dumps(payload, ensure_ascii=False))
        )
        parsed = parse_airdrop_payload(payload)
        all_rows.extend(parsed)
        if len(parsed) < limit:
            break
        start += len(parsed)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "status": args.status,
                    "row_count": len(all_rows),
                    "first_row": all_rows[0] if all_rows else None,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    insert_ingest_run_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_run_sql = load_sql("sys/finish_ingest_run.sql")
    insert_raw_sql = load_sql("raw/insert_api_response.sql")
    upsert_airdrop_sql = load_sql("biz/upsert_airdrop_event.sql")

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, fetch_one
    from crypto_research.utils.hash_utils import md5_text

    with get_connection(settings.database_url) as conn:
        run_row = fetch_one(
            conn,
            insert_ingest_run_sql,
            (
                "cmc",
                "cmc_airdrops",
                "WF_CMC_AIRDROPS",
                json.dumps({"status": args.status}, ensure_ascii=False),
                f"{settings.cmc_base_url}/v1/cryptocurrency/airdrops",
            ),
        )
        run_id = run_row["run_id"]

        try:
            for page_key, raw_text in fetched_raw:
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        "cmc_airdrops",
                        args.status,
                        None,
                        page_key,
                        raw_text,
                        md5_text(raw_text),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            row_params = [
                (
                    row["airdrop_id"],
                    row["project_name"],
                    row["description"],
                    row["status"],
                    row["coin_id"],
                    row["coin_symbol"],
                    row["coin_name"],
                    row["coin_slug"],
                    row["start_date"],
                    row["end_date"],
                    row["total_prize"],
                    row["winner_count"],
                    row["link"],
                    json.dumps({"status": args.status}, ensure_ascii=False),
                )
                for row in all_rows
                if row["airdrop_id"]
            ]
            if row_params:
                execute_many(conn, upsert_airdrop_sql, row_params)

            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success",
                    200,
                    len(all_rows),
                    len(all_rows),
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
                        "row_count": len(all_rows),
                        "airdrop_status": args.status,
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