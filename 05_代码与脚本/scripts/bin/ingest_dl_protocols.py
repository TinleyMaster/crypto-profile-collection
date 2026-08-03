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
        description="Ingest DefiLlama protocol list into src_dl.protocol_list."
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from crypto_research.clients.defillama_client import DefiLlamaClient
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, load_sql
    from crypto_research.parsers.dl_protocol import parse_dl_protocol_entry

    settings = get_settings(require_database=True)
    fetched_at = datetime.now(timezone.utc).isoformat()

    t0 = time.time()
    print(f"[ingest_dl_protocols] Fetching protocols...", flush=True)
    client = DefiLlamaClient(settings)
    payload = client.get_protocols()
    print(f"[ingest_dl_protocols] Fetched {len(payload)} protocols in {time.time()-t0:.1f}s", flush=True)

    t1 = time.time()
    parsed_rows = [parse_dl_protocol_entry(entry) for entry in payload]
    print(f"[ingest_dl_protocols] Parsed {len(parsed_rows)} rows in {time.time()-t1:.1f}s", flush=True)

    upsert_sql = load_sql("src_dl/upsert_protocol_list.sql")
    row_params = [
        (
            row["protocol_id"],
            row["name"],
            row["symbol"],
            row["slug"],
            row["category"],
            row["chain"],
            json.dumps(row["chains"], ensure_ascii=False),
            row["tvl"],
            row["change_1h"],
            row["change_1d"],
            row["change_7d"],
            row["url"],
            row["description"],
            row["address"],
            row["twitter"],
            row["cmc_id"],
            row["gecko_id"],
            None,  # raw_response_id
            fetched_at,
        )
        for row in parsed_rows
    ]

    t2 = time.time()
    print(f"[ingest_dl_protocols] Upserting {len(row_params)} rows...", flush=True)
    with get_connection(settings.database_url) as conn:
        execute_many(conn, upsert_sql, row_params)
    print(f"[ingest_dl_protocols] Upserted in {time.time()-t2:.1f}s", flush=True)

    print(json.dumps({
        "status": "success",
        "fetched_count": len(payload),
        "upserted_rows": len(parsed_rows),
        "fetched_at": fetched_at,
        "total_time_s": round(time.time() - t0, 1),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
