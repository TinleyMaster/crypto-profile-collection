"""Ingest CMC trending lists into biz.asset_trending.

Populates:
  - biz.asset_trending (trend_type: gainers / losers / trending / most_visited / new)

Sources:
  - /v1/cryptocurrency/trending/latest           (search volume)
  - /v1/cryptocurrency/trending/gainers-losers   (percent change, sort_dir asc=losers / desc=gainers)
  - /v1/cryptocurrency/trending/most-visited     (traffic)
  - /v1/cryptocurrency/listings/new              (recently listed)

Usage:
    python ingest_cmc_trending.py
    python ingest_cmc_trending.py --dry-run
    python ingest_cmc_trending.py --period 24h --limit 100
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC trending lists into biz.asset_trending."
    )
    parser.add_argument(
        "--period",
        default="24h",
        choices=["1h", "24h", "7d", "30d"],
        help="Trending time period. Default: 24h",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Rows per list. Default: 100",
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
    from crypto_research.parsers.cmc_analysis import parse_trending_payload

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    today = date.today()
    all_rows: list[dict] = []
    fetched_raw: list[tuple[str, str, str]] = []  # (endpoint_code, page_key, raw)

    # ═══ 各榜单拉取（失败单独降级，不阻塞其它榜单）═══
    def _safe_fetch(label: str, fn):
        try:
            return fn()
        except Exception as e:
            print(f"[{label}] 拉取失败，跳过: {e}", file=sys.stderr)
            return None

    # 1. Trending latest (搜索热度)
    payload = _safe_fetch("trending/latest", lambda: client.get_trending_latest(limit=args.limit, time_period=args.period))
    if payload:
        fetched_raw.append(("cmc_trending_latest", "page:all", json.dumps(payload, ensure_ascii=False)))
        all_rows.extend(parse_trending_payload(payload, "trending", args.period))

    # 2. Gainers (涨幅榜)
    payload = _safe_fetch("gainers", lambda: client.get_trending_gainers_losers(
        limit=args.limit, time_period=args.period, sort_dir="desc"))
    if payload:
        fetched_raw.append(("cmc_trending_gainers", "page:all", json.dumps(payload, ensure_ascii=False)))
        all_rows.extend(parse_trending_payload(payload, "gainers", args.period))

    # 3. Losers (跌幅榜)
    payload = _safe_fetch("losers", lambda: client.get_trending_gainers_losers(
        limit=args.limit, time_period=args.period, sort_dir="asc"))
    if payload:
        fetched_raw.append(("cmc_trending_losers", "page:all", json.dumps(payload, ensure_ascii=False)))
        all_rows.extend(parse_trending_payload(payload, "losers", args.period))

    # 4. Most visited (访问量)
    payload = _safe_fetch("most_visited", lambda: client.get_trending_most_visited(limit=args.limit, time_period=args.period))
    if payload:
        fetched_raw.append(("cmc_trending_most_visited", "page:all", json.dumps(payload, ensure_ascii=False)))
        all_rows.extend(parse_trending_payload(payload, "most_visited", args.period))

    # 5. New listings (新上市)
    payload = _safe_fetch("listings_new", lambda: client.get_listings_new(limit=args.limit))
    if payload:
        fetched_raw.append(("cmc_listings_new", "page:all", json.dumps(payload, ensure_ascii=False)))
        all_rows.extend(parse_trending_payload(payload, "new", args.period))

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "snapshot_date": today.isoformat(),
                    "row_count": len(all_rows),
                    "by_type": {t: sum(1 for r in all_rows if r["trend_type"] == t) for t in {"gainers", "losers", "trending", "most_visited", "new"}},
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
    upsert_trending_sql = load_sql("biz/upsert_asset_trending.sql")

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
                "cmc_trending_daily",
                "WF_CMC_TRENDING",
                json.dumps(
                    {
                        "snapshot_date": today.isoformat(),
                        "period": args.period,
                        "limit": args.limit,
                    },
                    ensure_ascii=False,
                ),
                f"{settings.cmc_base_url}/v1/cryptocurrency/trending/latest",
            ),
        )
        run_id = run_row["run_id"]

        try:
            # 写入 raw（每种榜单一条）
            for endpoint_code, page_key, raw_text in fetched_raw:
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        endpoint_code,
                        f"{today.isoformat()}|{args.period}",
                        None,
                        page_key,
                        raw_text,
                        md5_text(raw_text),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            row_params = [
                (
                    today,
                    row["trend_type"],
                    row["time_period"],
                    row["cmc_id"],
                    row["symbol"],
                    row["name"],
                    row["slug"],
                    row["rank_num"],
                    row["price_usd"],
                    row["market_cap"],
                    row["volume_24h"],
                    row["percent_change_24h"],
                    json.dumps({"snapshot_date": today.isoformat()}, ensure_ascii=False),
                )
                for row in all_rows
            ]
            if row_params:
                execute_many(conn, upsert_trending_sql, row_params)

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
                        "snapshot_date": today.isoformat(),
                        "by_type": {t: sum(1 for r in all_rows if r["trend_type"] == t) for t in {"gainers", "losers", "trending", "most_visited", "new"}},
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