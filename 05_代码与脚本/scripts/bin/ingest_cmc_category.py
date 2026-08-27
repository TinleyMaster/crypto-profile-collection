#!/usr/bin/env python3
"""
Ingest CMC cryptocurrency category aggregate data.

Populates:
  - src_cmc.cmc_category        (category list from /v1/cryptocurrency/categories)
  - src_cmc.cmc_category_member (category members from /v1/cryptocurrency/category)

Usage:
    python ingest_cmc_category.py
    python ingest_cmc_category.py --category-id 6051
    python ingest_cmc_category.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC category aggregates into src_cmc tables."
    )
    parser.add_argument(
        "--category-id",
        type=str,
        help="Only ingest a single category by CMC category id (MongoDB ObjectId string).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    parser.add_argument(
        "--member-limit",
        type=int,
        default=5000,
        help="Page limit for category member endpoint. Default: 5000 (CMC max).",
    )
    return parser


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_category_list(payload: dict) -> list[dict]:
    rows: list[dict] = []
    for item in payload.get("data", []) or []:
        rows.append(
            {
                "category_id": str(item.get("id") or ""),
                "category_name": item.get("name") or "",
                "title": item.get("title") or None,
                "description": item.get("description") or None,
                "num_tokens": _safe_int(item.get("num_tokens")),
                "market_cap": _safe_float(item.get("market_cap")),
                "volume_24h": _safe_float(item.get("volume_24h")),
                "last_updated": item.get("last_updated") or None,
            }
        )
    return rows


def _parse_category_members(payload: dict) -> list[dict]:
    rows: list[dict] = []
    data = payload.get("data") or {}
    snapshot_date = date.today()
    for coin in data.get("coins", []) or []:
        quote = (coin.get("quote") or {}).get("USD") or {}
        rows.append(
            {
                "cmc_id": _safe_int(coin.get("id")),
                "rank_in_category": _safe_int(coin.get("cmc_rank")),
                "market_cap": _safe_float(quote.get("market_cap")),
                "percent_change_24h": _safe_float(quote.get("percent_change_24h")),
                "snapshot_date": snapshot_date,
            }
        )
    return rows


def _record_run(
    conn,
    endpoint_code: str,
    request_params: dict,
    request_url: str,
    payload: dict,
    fetched_at: str,
) -> int:
    from crypto_research.db.upsert import fetch_one
    from crypto_research.utils.hash_utils import md5_text
    from crypto_research.utils.json_utils import stable_json_dumps

    payload_text = stable_json_dumps(payload)
    payload_hash = md5_text(payload_text)

    run_row = fetch_one(
        conn,
        load_sql("sys/insert_ingest_run.sql"),
        (
            "cmc",
            endpoint_code,
            "WF_CMC_CATEGORY",
            json.dumps(request_params, ensure_ascii=False),
            request_url,
        ),
    )
    run_id = run_row["run_id"]

    raw_row = fetch_one(
        conn,
        load_sql("raw/insert_api_response.sql"),
        (
            run_id,
            "cmc",
            endpoint_code,
            json.dumps(request_params, ensure_ascii=False, sort_keys=True),
            None,
            "page:single",
            payload_text,
            payload_hash,
            fetched_at,
        ),
    )
    return raw_row["response_id"]


def _finish_run(conn, run_id: int, status: str, rows: int, error: str | None = None) -> None:
    from crypto_research.db.upsert import fetch_one

    fetch_one(
        conn,
        load_sql("sys/finish_ingest_run.sql"),
        (
            status,
            200 if status == "success" else None,
            rows,
            rows,
            0,
            error,
            run_id,
        ),
    )


def load_sql(relative_path: str) -> str:
    from crypto_research.db.upsert import load_sql as _load_sql

    return _load_sql(relative_path)


def ingest_categories(client, conn, dry_run: bool) -> tuple[list[dict], int | None]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    payload = client.get_cryptocurrency_categories(start=1, limit=5000)
    category_rows = _parse_category_list(payload)

    if dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "endpoint": "categories",
                    "row_count": len(category_rows),
                    "first_row": category_rows[0] if category_rows else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return category_rows, None

    response_id = _record_run(
        conn,
        "cmc_categories",
        {"start": 1, "limit": 5000},
        f"{client.settings.cmc_base_url}/v1/cryptocurrency/categories",
        payload,
        fetched_at,
    )

    upsert_sql = load_sql("src_cmc/upsert_cmc_category.sql")
    params = [
        (
            row["category_id"],
            row["category_name"],
            row["title"],
            row["description"],
            row["num_tokens"],
            row["market_cap"],
            row["volume_24h"],
            row["last_updated"],
            response_id,
            fetched_at,
        )
        for row in category_rows
    ]
    from crypto_research.db.upsert import execute_many

    execute_many(conn, upsert_sql, params)
    return category_rows, response_id


def ingest_category_members(
    client,
    conn,
    category_id: str,
    category_name: str,
    limit: int,
    dry_run: bool,
) -> tuple[int, int | None]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    all_rows: list[dict] = []
    start = 1
    max_pages = 100  # safety guard
    page = 0
    request_url = f"{client.settings.cmc_base_url}/v1/cryptocurrency/category"

    while page < max_pages:
        payload = client.get_cryptocurrency_category(
            category_id=category_id, start=start, limit=limit
        )
        rows = _parse_category_members(payload)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < limit:
            break
        start += limit
        page += 1

    if dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "endpoint": "category",
                    "category_id": category_id,
                    "category_name": category_name,
                    "row_count": len(all_rows),
                    "first_row": all_rows[0] if all_rows else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return len(all_rows), None

    # Record a single run/response for all pages of this category.
    response_id = _record_run(
        conn,
        "cmc_category",
        {"id": category_id, "limit": limit},
        request_url,
        {"category_id": category_id, "rows": len(all_rows)},
        fetched_at,
    )

    upsert_sql = load_sql("src_cmc/upsert_cmc_category_member.sql")
    params = [
        (
            str(category_id),
            row["cmc_id"],
            row["snapshot_date"],
            row["rank_in_category"],
            row["market_cap"],
            row["percent_change_24h"],
            response_id,
        )
        for row in all_rows
        if row["cmc_id"] is not None
    ]
    from crypto_research.db.upsert import execute_many

    execute_many(conn, upsert_sql, params)
    return len(all_rows), response_id


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.cmc_client import CMCClient
    from crypto_research.config import get_settings

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    if args.dry_run:
        category_rows, _ = ingest_categories(client, None, dry_run=True)
        if args.category_id:
            # Only test member endpoint if category_id is numeric (CMC API requirement)
            if args.category_id.isdigit():
                ingest_category_members(
                    client, None, args.category_id, "dry-run", args.member_limit, dry_run=True
                )
            else:
                print(f"Skipping member test: category_id '{args.category_id}' is not numeric (CMC API requires numeric ID)")
        else:
            # Find first numeric category_id for member test
            numeric_cat = next((cat for cat in category_rows[:10] if cat["category_id"].isdigit()), None)
            if numeric_cat:
                ingest_category_members(
                    client,
                    None,
                    numeric_cat["category_id"],
                    numeric_cat["category_name"],
                    args.member_limit,
                    dry_run=True,
                )
            else:
                print("Skipping member test: no numeric category_id found in first 10 categories")
        return 0

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection

    with get_connection(settings.database_url) as conn:
        category_rows, _ = ingest_categories(client, conn, dry_run=False)
        target_ids = [args.category_id] if args.category_id else None
        processed = 0
        failed = 0

        for cat in category_rows:
            cat_id = cat["category_id"]
            if target_ids and cat_id not in target_ids:
                continue
            # Skip non-numeric category IDs (CMC member API requires numeric ID)
            if not cat_id.isdigit():
                print(f"  category {cat_id} ({cat['category_name']}): skipped (non-numeric ID)")
                continue
            try:
                count, _ = ingest_category_members(
                    client, conn, cat_id, cat["category_name"], args.member_limit, dry_run=False
                )
                print(
                    f"  category {cat_id} ({cat['category_name']}): {count} members"
                )
                processed += 1
            except Exception as exc:
                print(
                    f"  category {cat_id} ({cat['category_name']}): failed - {exc}",
                    file=sys.stderr,
                )
                failed += 1
                # do not stop; other categories may succeed

    print(
        json.dumps(
            {
                "status": "success" if failed == 0 else "partial",
                "categories": len(category_rows),
                "processed": processed,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
