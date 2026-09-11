"""Ingest CMC market pairs into src_cmc.cmc_market_pair_snapshot.

Populates:
  - src_cmc.cmc_market_pair_snapshot (from /v2/cryptocurrency/market-pairs/latest)

Usage:
    python ingest_cmc_market_pairs.py --top 200
    python ingest_cmc_market_pairs.py --dry-run
    python ingest_cmc_market_pairs.py --cmc-id 1
"""
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
        description="Ingest CMC market pairs into src_cmc.cmc_market_pair_snapshot."
    )
    parser.add_argument(
        "--top",
        type=int,
        default=200,
        help="Only ingest top N assets by CMC rank. Default: 200.",
    )
    parser.add_argument(
        "--cmc-id",
        type=int,
        default=None,
        help="Ingest a single asset by CMC id (overrides --top).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Market pairs per asset per call (max 5000). Default: 100.",
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
    from crypto_research.parsers.cmc_analysis import parse_market_pairs_payload

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    # 获取待处理 cmc_ids
    all_ids: list[int] = []
    if not args.dry_run:
        from crypto_research.db.conn import get_connection

        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                if args.cmc_id:
                    all_ids = [args.cmc_id]
                else:
                    cur.execute(
                        """
                        SELECT cmc_id
                        FROM src_cmc.cmc_asset_map
                        WHERE is_active = TRUE
                          AND rank_num IS NOT NULL
                        ORDER BY rank_num NULLS LAST, cmc_id
                        LIMIT %s
                        """,
                        (args.top,),
                    )
                    all_ids = [r[0] for r in cur.fetchall()]
    else:
        all_ids = [args.cmc_id] if args.cmc_id else list(range(1, min(args.top, 20) + 1))

    if not all_ids:
        print(json.dumps({"status": "noop", "reason": "no cmc_ids"}, ensure_ascii=False))
        return 0

    # ═══ 逐个拉取（单币种接口，不可并发，需限速）═══
    all_rows: list[dict] = []
    for cmc_id in all_ids:
        try:
            payload = client.get_market_pairs(ids=[cmc_id], limit=args.limit)
        except Exception as e:
            print(f"[market_pairs] cmc_id={cmc_id} 失败: {e}", file=sys.stderr)
            continue
        parsed = parse_market_pairs_payload(payload)
        all_rows.extend(parsed)
        print(f"[market_pairs] cmc_id={cmc_id} 解析 {len(parsed)} 对", file=sys.stderr)
        time.sleep(0.5)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
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
    upsert_market_pairs_sql = load_sql("src_cmc/upsert_cmc_market_pair_snapshot.sql")

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
                "cmc_market_pairs",
                "WF_CMC_MARKET_PAIRS",
                json.dumps({"top": args.top, "cmc_id": args.cmc_id}, ensure_ascii=False),
                f"{settings.cmc_base_url}/v2/cryptocurrency/market-pairs/latest",
            ),
        )
        run_id = run_row["run_id"]

        try:
            for cmc_id in all_ids:
                request_key = f"cmc_id={cmc_id}"
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        "cmc_market_pairs",
                        request_key,
                        None,
                        "page:all",
                        None,
                        md5_text(request_key),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # 外键保护
            with conn.cursor() as cur:
                cmc_ids = {r["cmc_id"] for r in all_rows}
                if cmc_ids:
                    cur.execute(
                        "SELECT cmc_id FROM src_cmc.cmc_asset_map WHERE cmc_id = ANY(%s)",
                        (list(cmc_ids),),
                    )
                    valid_ids = {r[0] for r in cur.fetchall()}
                else:
                    valid_ids = set()

            filtered = [r for r in all_rows if r["cmc_id"] in valid_ids]
            row_params = [
                (
                    row["cmc_id"],
                    row["snapshot_time"],
                    row["exchange_name"],
                    row["market_pair"],
                    row["market_type"],
                    row["category"],
                    row["pair_base_symbol"],
                    row["pair_quote_symbol"],
                    row["price"],
                    row["volume_24h"],
                    row["liquidity_usd"],
                    row["market_url"],
                    row["outlier_score"],
                    row["effective_liquidity"],
                    None,
                )
                for row in filtered
            ]
            if row_params:
                execute_many(conn, upsert_market_pairs_sql, row_params)

            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success",
                    200,
                    len(all_rows),
                    len(filtered),
                    len(all_rows) - len(filtered),
                    None,
                    run_id,
                ),
            )

            print(
                json.dumps(
                    {
                        "status": "success",
                        "run_id": run_id,
                        "row_count": len(filtered),
                        "skipped_unknown_cmc_id": len(all_rows) - len(filtered),
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