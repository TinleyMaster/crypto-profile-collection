from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CoinGecko coin detail info into src_cg.coin_info."
    )
    parser.add_argument(
        "--coin-id",
        help="Single CoinGecko coin id, e.g. 'bitcoin'.",
    )
    parser.add_argument(
        "--from-list-missing",
        action="store_true",
        help="Load missing coin_ids from src_cg.coin_list minus src_cg.coin_info.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Batch size when using --from-list-missing. Default: 10",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.coingecko_client import CoinGeckoClient
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, fetch_one, load_sql
    from crypto_research.parsers.cg_coin_info import parse_cg_coin_info_payload
    from crypto_research.utils.hash_utils import md5_text
    from crypto_research.utils.json_utils import stable_json_dumps

    settings = get_settings(require_database=not args.dry_run)

    coin_ids: list[str]
    if args.coin_id:
        coin_ids = [args.coin_id.strip()]
    elif args.from_list_missing:
        if not settings.database_url:
            raise RuntimeError(
                "DATABASE_URL is required when using --from-list-missing"
            )
        select_missing_sql = load_sql("src_cg/select_missing_coin_info_ids.sql")
        with get_connection(settings.database_url) as conn:
            result = fetch_one(conn, select_missing_sql, (args.limit,))
        coin_ids = result.get("coin_ids") or []
        if not coin_ids:
            print(
                json.dumps(
                    {"status": "noop", "message": "No missing coin_info ids found."},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(
            f"[ingest_cg_coin_info] Processing {len(coin_ids)} coins: {coin_ids[:5]}{'...' if len(coin_ids) > 5 else ''}",
            flush=True,
        )
    else:
        raise ValueError("Either --coin-id or --from-list-missing must be provided")

    client = CoinGeckoClient(settings)
    upsert_sql = load_sql("src_cg/upsert_coin_info.sql")
    insert_raw_sql = load_sql("raw/insert_api_response.sql")
    insert_ingest_run_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_run_sql = load_sql("sys/finish_ingest_run.sql")

    if args.dry_run:
        for coin_id in coin_ids:
            payload = client.get_coin_by_id(coin_id)
            parsed = parse_cg_coin_info_payload(payload, raw_response_id=None)
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "coin_id": coin_id,
                        "name": parsed.get("name"),
                        "symbol": parsed.get("symbol"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    fetched_at = datetime.now(timezone.utc).isoformat()
    with get_connection(settings.database_url) as conn:
        run_row = fetch_one(
            conn,
            insert_ingest_run_sql,
            (
                "cg",
                "coin_info",
                "WF_CG_COIN_INFO_BATCH",
                json.dumps({"coin_ids": coin_ids}, ensure_ascii=False),
                f"{settings.coingecko_base_url}/coins/{{id}}",
            ),
        )
        run_id = run_row["run_id"]

        success_count = 0
        fail_count = 0
        last_error: str | None = None

        try:
            for idx, coin_id in enumerate(coin_ids):
                try:
                    payload = client.get_coin_by_id(coin_id)
                    payload_text = stable_json_dumps(payload)
                    payload_hash = md5_text(payload_text)

                    raw_row = fetch_one(
                        conn,
                        insert_raw_sql,
                        (
                            run_id,
                            "cg",
                            "coin_info",
                            f"id={coin_id}",
                            None,
                            "page:single",
                            payload_text,
                            payload_hash,
                            fetched_at,
                        ),
                    )
                    raw_response_id = raw_row["response_id"]

                    parsed = parse_cg_coin_info_payload(
                        payload, raw_response_id=raw_response_id
                    )
                    row_params = [
                        (
                            parsed["coin_id"],
                            parsed["symbol"],
                            parsed["name"],
                            parsed["description"],
                            parsed["homepage_url"],
                            parsed["image"],
                            parsed["genesis_date"],
                            parsed["market_cap_rank"],
                            parsed["coingecko_rank"],
                            json.dumps(parsed["categories"], ensure_ascii=False),
                            json.dumps(parsed["platforms"], ensure_ascii=False),
                            json.dumps(parsed["links"], ensure_ascii=False),
                            raw_response_id,
                            fetched_at,
                        )
                    ]
                    execute_many(conn, upsert_sql, row_params)
                    success_count += 1

                    progress = idx + 1
                    if progress % 10 == 0 or progress == len(coin_ids):
                        print(
                            f"[ingest_cg_coin_info] Progress: {progress}/{len(coin_ids)} ok={success_count} fail={fail_count}",
                            flush=True,
                        )
                except Exception as exc:
                    fail_count += 1
                    last_error = str(exc)
                    print(
                        f"[ingest_cg_coin_info] FAIL {coin_id}: {last_error[:200]}",
                        flush=True,
                    )
                    # rollback current sub-transaction but continue
                    conn.rollback()

            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success" if fail_count == 0 else "partial",
                    200,
                    len(coin_ids),
                    success_count,
                    fail_count,
                    last_error,
                    run_id,
                ),
            )

            print(
                json.dumps(
                    {
                        "status": "success" if fail_count == 0 else "partial",
                        "run_id": run_id,
                        "total": len(coin_ids),
                        "success": success_count,
                        "failed": fail_count,
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
                    len(coin_ids),
                    success_count,
                    fail_count,
                    str(exc),
                    run_id,
                ),
            )
            raise


if __name__ == "__main__":
    raise SystemExit(main())
