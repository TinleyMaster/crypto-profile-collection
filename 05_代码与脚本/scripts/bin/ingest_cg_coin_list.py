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
        description="Ingest CoinGecko coin list into src_cg.coin_list."
    )
    parser.add_argument(
        "--include-platforms",
        action="store_true",
        help="Include platform metadata in the response.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.coingecko_client import CoinGeckoClient
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, load_sql
    from crypto_research.parsers.cg_coin_list import parse_cg_coin_list_entry

    settings = get_settings(require_database=True)

    client = CoinGeckoClient(settings)
    import time

    t0 = time.time()
    print(f"[ingest_cg_coin_list] Fetching coin list...", flush=True)
    payload = client.get_coins_list(include_platform=args.include_platforms)
    print(
        f"[ingest_cg_coin_list] Fetched {len(payload)} coins in {time.time() - t0:.1f}s",
        flush=True,
    )
    fetched_at = datetime.now(timezone.utc).isoformat()

    t1 = time.time()
    parsed_rows = [parse_cg_coin_list_entry(entry) for entry in payload]
    print(
        f"[ingest_cg_coin_list] Parsed {len(parsed_rows)} rows in {time.time() - t1:.1f}s",
        flush=True,
    )

    upsert_sql = load_sql("src_cg/upsert_coin_list.sql")
    row_params = [
        (
            row["coin_id"],
            row["symbol"],
            row["name"],
            json.dumps(row["platforms"], ensure_ascii=False),
        )
        for row in parsed_rows
    ]

    t2 = time.time()
    print(f"[ingest_cg_coin_list] Upserting {len(row_params)} rows...", flush=True)
    with get_connection(settings.database_url) as conn:
        execute_many(conn, upsert_sql, row_params)
    print(f"[ingest_cg_coin_list] Upserted in {time.time() - t2:.1f}s", flush=True)

    print(
        json.dumps(
            {
                "status": "success",
                "fetched_count": len(payload),
                "upserted_rows": len(parsed_rows),
                "fetched_at": fetched_at,
                "total_time_s": round(time.time() - t0, 1),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
