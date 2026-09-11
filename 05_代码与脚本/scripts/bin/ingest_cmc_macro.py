"""Ingest CMC macro indicators into biz tables.

Populates:
  - biz.global_metric_daily    (/v1/global-metrics/quotes/latest)
  - biz.fear_greed_daily       (/v3/fear-and-greed)
  - biz.altcoin_season_daily   (/v1/altcoin-season-index, trial-pro-api)

Usage:
    python ingest_cmc_macro.py
    python ingest_cmc_macro.py --dry-run
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
        description="Ingest CMC macro indicators (global metrics / fear&greed / altcoin season)."
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
    from crypto_research.parsers.cmc_analysis import (
        parse_altcoin_season_payload,
        parse_fear_greed_payload,
        parse_global_metrics_payload,
    )

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    today = date.today()
    errors: dict[str, str] = {}
    # 每个指标独立存储：raw_payload + rows，一个失败不影响其它
    metric: dict | None = None
    fg: dict | None = None
    as_: dict | None = None

    def _safe(label: str, fn):
        try:
            return fn()
        except Exception as e:
            errors[label] = str(e)
            print(f"[{label}] 拉取失败，跳过: {e}", file=sys.stderr)
            return None

    # ═══ 1. 全球市场指标 ═══
    payload = _safe("global_metrics", client.get_global_metrics)
    if payload:
        parsed = parse_global_metrics_payload(payload)
        if parsed:
            metric = {
                "payload": payload,
                "parsed": parsed,
                "rows": [
                    (
                        today,
                        parsed["total_market_cap"],
                        parsed["total_volume_24h"],
                        parsed["btc_dominance"],
                        parsed["eth_dominance"],
                        parsed["stablecoin_market_cap"],
                        parsed["total_cryptocurrencies"],
                        parsed["active_cryptocurrencies"],
                        json.dumps({"global_metrics": parsed}, ensure_ascii=False),
                    )
                ],
            }

    # ═══ 2. 恐贪指数 ═══
    payload = _safe("fear_greed", client.get_fear_greed)
    if payload:
        parsed = parse_fear_greed_payload(payload)
        if parsed and parsed["value"] is not None:
            fg = {
                "payload": payload,
                "parsed": parsed,
                "rows": [
                    (
                        today,
                        parsed["value"],
                        parsed["value_classification"],
                        json.dumps({"fear_greed": parsed}, ensure_ascii=False),
                    )
                ],
            }

    # ═══ 3. 山寨季指数 ═══
    payload = _safe("altcoin_season", client.get_altcoin_season)
    if payload:
        parsed = parse_altcoin_season_payload(payload)
        if parsed and parsed["value"] is not None:
            as_ = {
                "payload": payload,
                "parsed": parsed,
                "rows": [
                    (
                        today,
                        parsed["value"],
                        json.dumps({"altcoin_season": parsed}, ensure_ascii=False),
                    )
                ],
            }

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "metric_date": today.isoformat(),
                    "global_metric": metric["parsed"] if metric else None,
                    "fear_greed": fg["parsed"] if fg else None,
                    "altcoin_season": as_["parsed"] if as_ else None,
                    "errors": errors,
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
    upsert_metric_sql = load_sql("biz/upsert_global_metric_daily.sql")
    upsert_fg_sql = load_sql("biz/upsert_fear_greed_daily.sql")
    upsert_as_sql = load_sql("biz/upsert_altcoin_season_daily.sql")

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
                "cmc_macro_daily",
                "WF_CMC_MACRO",
                json.dumps({"metric_date": today.isoformat()}, ensure_ascii=False),
                f"{settings.cmc_base_url}/v1/global-metrics/quotes/latest",
            ),
        )
        run_id = run_row["run_id"]

        try:
            total_rows = 0

            for endpoint_code, block in (
                ("cmc_global_metrics", metric),
                ("cmc_fear_greed", fg),
                ("cmc_altcoin_season", as_),
            ):
                if not block:
                    continue
                payload_text = json.dumps(block["payload"], ensure_ascii=False)
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        endpoint_code,
                        today.isoformat(),
                        None,
                        "page:all",
                        payload_text,
                        md5_text(payload_text),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            if metric:
                execute_many(conn, upsert_metric_sql, metric["rows"])
                total_rows += len(metric["rows"])
            if fg:
                execute_many(conn, upsert_fg_sql, fg["rows"])
                total_rows += len(fg["rows"])
            if as_:
                execute_many(conn, upsert_as_sql, as_["rows"])
                total_rows += len(as_["rows"])

            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success",
                    200,
                    total_rows,
                    total_rows,
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
                        "row_count": total_rows,
                        "global_metric": bool(metric),
                        "fear_greed": bool(fg),
                        "altcoin_season": bool(as_),
                        "errors": errors,
                        "metric_date": today.isoformat(),
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