"""ETL: 从 src_cmc.cmc_asset_quote_snapshot 聚合成日级，写入 biz.asset_market_daily。

聚合规则：
- 按 (asset_id, date) 分组，取当日 quote_time 最晚的一条快照作为日收盘价
- source_code = 'cmc'
- 幂等：ON CONFLICT 更新所有数值字段

用法：
    python etl_asset_market_daily_from_cmc.py              # 全量回填（所有已有快照）
    python etl_asset_market_daily_from_cmc.py --days 7     # 只回填最近 7 天
    python etl_asset_market_daily_from_cmc.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ETL CMC quote snapshots into biz.asset_market_daily (daily close)."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only backfill last N days. Default: all available data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows only, do not insert.",
    )
    return parser


def _get_db_conn(settings):
    return psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )


def etl_cmc_to_daily(days: int | None, dry_run: bool) -> dict:
    """执行 ETL，返回统计信息。"""
    settings = get_settings(require_database=True)
    with _get_db_conn(settings) as conn:
        with conn.cursor() as cur:
            # 确定时间范围
            date_filter = ""
            params: tuple = ()
            if days is not None:
                start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
                date_filter = "WHERE q.quote_time >= %s"
                params = (start_date,)

            # 用 ROW_NUMBER 取每日最后一条快照（按 cmc_id + 日期分组）
            # JOIN coin_basic 拿到 asset_id
            sql = f"""
                WITH ranked AS (
                    SELECT
                        cb.asset_id,
                        q.cmc_id,
                        DATE(q.quote_time AT TIME ZONE 'UTC') AS market_date,
                        q.price_usd,
                        q.market_cap,
                        q.fdv,
                        q.circulating_supply,
                        q.total_supply,
                        q.volume_24h,
                        q.percent_change_24h AS change_24h,
                        q.percent_change_7d AS change_7d,
                        q.quote_time,
                        ROW_NUMBER() OVER (
                            PARTITION BY cb.asset_id, DATE(q.quote_time AT TIME ZONE 'UTC')
                            ORDER BY q.quote_time DESC
                        ) AS rn
                    FROM src_cmc.cmc_asset_quote_snapshot q
                    JOIN biz.coin_basic cb ON cb.cmc_id = q.cmc_id
                    {date_filter}
                )
                SELECT
                    asset_id,
                    market_date,
                    price_usd,
                    market_cap,
                    fdv,
                    circulating_supply,
                    total_supply,
                    volume_24h,
                    change_24h,
                    change_7d
                FROM ranked
                WHERE rn = 1
                  AND asset_id IS NOT NULL
                ORDER BY market_date, asset_id
            """

            cur.execute(sql, params)
            rows = cur.fetchall()

            if not rows:
                return {"inserted": 0, "updated": 0, "total": 0, "date_range": None}

            if dry_run:
                dates = [r[1] for r in rows]
                return {
                    "total": len(rows),
                    "date_from": str(min(dates)),
                    "date_to": str(max(dates)),
                    "dry_run": True,
                }

            # 写入 biz.asset_market_daily
            inserted = 0
            updated = 0
            with conn.cursor() as wcur:
                for row in rows:
                    (
                        asset_id, market_date, price_usd, market_cap, fdv,
                        circulating_supply, total_supply, volume_24h,
                        change_24h, change_7d,
                    ) = row
                    wcur.execute(
                        """
                        INSERT INTO biz.asset_market_daily
                            (asset_id, market_date, source_code, price_usd,
                             market_cap, fdv, circulating_supply, total_supply,
                             volume_24h, change_24h, change_7d, raw_ref)
                        VALUES (%s, %s, 'cmc', %s, %s, %s, %s, %s, %s, %s, %s,
                                '{"source": "cmc_quote_snapshot"}'::jsonb)
                        ON CONFLICT (asset_id, market_date, source_code) DO UPDATE SET
                            price_usd = EXCLUDED.price_usd,
                            market_cap = EXCLUDED.market_cap,
                            fdv = EXCLUDED.fdv,
                            circulating_supply = EXCLUDED.circulating_supply,
                            total_supply = EXCLUDED.total_supply,
                            volume_24h = EXCLUDED.volume_24h,
                            change_24h = EXCLUDED.change_24h,
                            change_7d = EXCLUDED.change_7d,
                            updated_at = NOW()
                        """,
                        (
                            asset_id, market_date, price_usd, market_cap, fdv,
                            circulating_supply, total_supply, volume_24h,
                            change_24h, change_7d,
                        ),
                    )
                    if wcur.statusmessage and "INSERT 0 1" in wcur.statusmessage:
                        inserted += 1
                    else:
                        updated += 1

            conn.commit()

            dates = [r[1] for r in rows]
            return {
                "total": len(rows),
                "inserted": inserted,
                "updated": updated,
                "date_from": str(min(dates)),
                "date_to": str(max(dates)),
            }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    print(f"[ETL] asset_market_daily from CMC snapshots")
    if args.days:
        print(f"[ETL] Range: last {args.days} days")
    else:
        print(f"[ETL] Range: all available data")

    result = etl_cmc_to_daily(days=args.days, dry_run=args.dry_run)

    if result.get("dry_run"):
        print(f"[DRY-RUN] Would process {result['total']} rows")
        print(f"[DRY-RUN] Date range: {result['date_from']} ~ {result['date_to']}")
    else:
        print(f"[ETL] Done: {result['total']} rows "
              f"(inserted={result.get('inserted', 0)}, updated={result.get('updated', 0)})")
        if result.get("date_from"):
            print(f"[ETL] Date range: {result['date_from']} ~ {result['date_to']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
