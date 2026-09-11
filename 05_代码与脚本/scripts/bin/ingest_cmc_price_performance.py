"""Ingest CMC price performance stats (ATH/ATL) into src_cmc and biz tables.

Populates:
  - src_cmc.cmc_asset_perf_stats   (from /v2/cryptocurrency/price-performance-stats/latest)
  - biz.asset_perf_daily           (汇总：all_time 周期抽出 ATH/ATL/回撤，按资产每日一行)

Usage:
    python ingest_cmc_price_performance.py --top 1000
    python ingest_cmc_price_performance.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC price performance stats (ATH/ATL) into src_cmc/biz tables."
    )
    parser.add_argument(
        "--top",
        type=int,
        default=1000,
        help="Only ingest top N assets by CMC rank. Default: 1000.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="CMC IDs per API call (max 100). Default: 100.",
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
    from crypto_research.parsers.cmc_analysis import parse_perf_stats_payload

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    # 获取待处理的 cmc_ids
    all_ids: list[int] = []
    if not args.dry_run:
        from crypto_research.db.conn import get_connection

        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
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
        all_ids = list(range(1, min(args.top, 100) + 1))

    if not all_ids:
        print(json.dumps({"status": "noop", "reason": "no cmc_ids in cmc_asset_map"}, ensure_ascii=False))
        return 0

    # ═══ 分批拉取 ═══
    all_rows: list[dict] = []
    batch_size = min(args.batch_size, 100)
    for batch_start in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[batch_start: batch_start + batch_size]
        try:
            payload = client.get_price_performance_stats(
                ids=batch_ids,
                time_period="all_time",
            )
        except Exception as e:
            print(f"[perf] 批次 {batch_start} 失败: {e}", file=sys.stderr)
            continue
        parsed = parse_perf_stats_payload(payload)
        all_rows.extend(parsed)
        print(f"[perf] 批次 {batch_start + 1}/{len(all_ids) // batch_size + 1} 解析 {len(parsed)} 行", file=sys.stderr)
        time.sleep(0.3)

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
    upsert_perf_sql = load_sql("src_cmc/upsert_cmc_asset_perf_stats.sql")
    upsert_perf_daily_sql = load_sql("biz/upsert_asset_perf_daily.sql")

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
                "cmc_price_performance",
                "WF_CMC_PRICE_PERF",
                json.dumps({"top": args.top}, ensure_ascii=False),
                f"{settings.cmc_base_url}/v2/cryptocurrency/price-performance-stats/latest",
            ),
        )
        run_id = run_row["run_id"]

        try:
            # raw（每批次）
            for batch_start in range(0, len(all_ids), batch_size):
                batch_ids = all_ids[batch_start: batch_start + batch_size]
                request_key = f"ids={','.join(str(i) for i in batch_ids[:10])}..."
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        "cmc_price_performance",
                        request_key,
                        None,
                        "page:batch",
                        None,
                        md5_text(request_key),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # 过滤外键保护
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
                    row["time_period"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["percent_change"],
                    row["price_change"],
                    row["open_timestamp"],
                    row["high_timestamp"],
                    row["low_timestamp"],
                    row["close_timestamp"],
                    None,
                )
                for row in filtered
            ]
            if row_params:
                execute_many(conn, upsert_perf_sql, row_params)

            # 汇总 all_time 到 biz.asset_perf_daily（按 asset_id 映射）
            all_time_rows = [r for r in filtered if r["time_period"] == "all_time"]
            perf_daily_params: list[tuple] = []
            if all_time_rows:
                # cmc_id -> asset_id 映射
                with conn.cursor() as cur:
                    cmc_ids = {r["cmc_id"] for r in all_time_rows}
                    cur.execute(
                        """
                        SELECT source_asset_key::BIGINT AS cmc_id, asset_id
                        FROM core.asset_source_map
                        WHERE source_code = 'cmc'
                          AND source_asset_key::BIGINT = ANY(%s)
                        """,
                        (list(cmc_ids),),
                    )
                    cmc_to_asset = {r[0]: r[1] for r in cur.fetchall()}

                perf_daily_params = []
                for row in all_time_rows:
                    asset_id = cmc_to_asset.get(row["cmc_id"])
                    if asset_id is None:
                        continue
                    high = row["high"]
                    ath_ts = row["high_timestamp"]
                    low = row["low"]
                    atl_ts = row["low_timestamp"]
                    close = row["close"]
                    drawdown = None
                    if high and close is not None and high > 0:
                        drawdown = (close - high) / high * 100
                    perf_daily_params.append(
                        (
                            asset_id,
                            date.today(),
                            "cmc",
                            high,
                            ath_ts,
                            low,
                            atl_ts,
                            drawdown,
                            json.dumps(
                                {"cmc_id": row["cmc_id"], "time_period": "all_time"},
                                ensure_ascii=False,
                            ),
                        )
                    )
                if perf_daily_params:
                    execute_many(conn, upsert_perf_daily_sql, perf_daily_params)

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
                        "perf_daily_rows": len(perf_daily_params),
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