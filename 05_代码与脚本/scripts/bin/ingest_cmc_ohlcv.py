"""Ingest CMC OHLCV (K线) into src_cmc.cmc_asset_ohlcv.

Populates:
  - src_cmc.cmc_asset_ohlcv (from /v2/cryptocurrency/ohlcv/historical)

支持全市场币种日线回填（含 DEX-only token），按 cmc_asset_map 分批拉取。

Usage:
    python ingest_cmc_ohlcv.py --days 365 --top 1000        # top 1000 最近一年日线
    python ingest_cmc_ohlcv.py --days 90 --batch-size 50    # 分批拉取
    python ingest_cmc_ohlcv.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC historical OHLCV into src_cmc.cmc_asset_ohlcv."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Number of days of history to ingest. Default: 365.",
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
        default=50,
        help="CMC IDs per API call (max 100). Default: 50.",
    )
    parser.add_argument(
        "--interval",
        default="daily",
        choices=["daily", "hourly"],
        help="OHLCV time period. Default: daily.",
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
    from crypto_research.parsers.cmc_analysis import parse_ohlcv_historical_payload

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    today = date.today()
    time_start = (today - timedelta(days=args.days)).isoformat()
    # count = 天数 + 1，跳过当前不完整周期（CMC 建议）
    count = args.days + 1

    # 获取待处理的 cmc_ids（按 rank 排序）
    all_ids: list[tuple[int, int]] = []
    if not args.dry_run:
        from crypto_research.db.conn import get_connection

        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cmc_id, rank_num
                    FROM src_cmc.cmc_asset_map
                    WHERE is_active = TRUE
                      AND rank_num IS NOT NULL
                    ORDER BY rank_num NULLS LAST, cmc_id
                    LIMIT %s
                    """,
                    (args.top,),
                )
                all_ids = [(r[0], r[1]) for r in cur.fetchall()]
    else:
        all_ids = [(i, i) for i in range(1, min(args.top, 100) + 1)]

    if not all_ids:
        print(json.dumps({"status": "noop", "reason": "no cmc_ids in cmc_asset_map"}, ensure_ascii=False))
        return 0

    # ═══ 分批拉取 + 解析 ═══
    all_rows: list[dict] = []
    batch_size = min(args.batch_size, 100)
    for batch_start in range(0, len(all_ids), batch_size):
        batch = all_ids[batch_start: batch_start + batch_size]
        batch_ids = [cid for cid, _ in batch]
        try:
            payload = client.get_ohlcv_historical(
                ids=batch_ids,
                time_period=args.interval,
                time_start=time_start,
                count=count,
                interval=args.interval,
            )
        except Exception as e:
            print(f"[ohlcv] 批次 {batch_start} 失败: {e}", file=sys.stderr)
            continue
        parsed = parse_ohlcv_historical_payload(payload, time_period=args.interval)
        all_rows.extend(parsed)
        print(f"[ohlcv] 批次 {batch_start + 1}/{len(all_ids) // batch_size + 1} 解析 {len(parsed)} 行", file=sys.stderr)
        time.sleep(0.3)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "time_start": time_start,
                    "row_count": len(all_rows),
                    "first_row": all_rows[0] if all_rows else None,
                    "last_row": all_rows[-1] if all_rows else None,
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
    upsert_ohlcv_sql = load_sql("src_cmc/upsert_cmc_asset_ohlcv.sql")

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
                "cmc_ohlcv_historical",
                "WF_CMC_OHLCV",
                json.dumps(
                    {
                        "days": args.days,
                        "top": args.top,
                        "interval": args.interval,
                    },
                    ensure_ascii=False,
                ),
                f"{settings.cmc_base_url}/v2/cryptocurrency/ohlcv/historical",
            ),
        )
        run_id = run_row["run_id"]

        try:
            # 每批次 raw 响应
            for batch_start in range(0, len(all_ids), batch_size):
                batch_ids = [cid for cid, _ in all_ids[batch_start: batch_start + batch_size]]
                request_key = f"ids={','.join(str(i) for i in batch_ids[:10])}...|{time_start}"
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        "cmc_ohlcv_historical",
                        request_key,
                        None,
                        "page:batch",
                        None,  # payload 未保留，仅记录请求 key
                        md5_text(request_key),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # 过滤掉不在 cmc_asset_map 的币（外键保护）
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
                    row["time_open"],
                    row["time_period"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row["market_cap"],
                    None,
                )
                for row in filtered
            ]
            if row_params:
                execute_many(conn, upsert_ohlcv_sql, row_params)

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
                        "time_start": time_start,
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