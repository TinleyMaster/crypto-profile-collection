"""ETL: 从 src_cmc.cmc_asset_quote_snapshot 聚合成日级，写入 biz.asset_market_daily。

聚合规则：
- 按 (asset_id, date) 分组，取当日 quote_time 最晚的一条快照作为日收盘价
- source_code = 'cmc'
- 幂等：ON CONFLICT 更新所有数值字段
- 纯 SQL 批量执行（性能远优于逐行 INSERT）

用法：
    python etl_asset_market_daily_from_cmc.py              # 全量回填（所有已有快照）
    python etl_asset_market_daily_from_cmc.py --days 7     # 只回填最近 7 天
    python etl_asset_market_daily_from_cmc.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


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


def etl_cmc_to_daily(days: int | None, dry_run: bool) -> dict:
    """执行 ETL，返回统计信息。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            date_filter = ""
            params: tuple = ()
            if days is not None:
                start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
                date_filter = "WHERE q.quote_time >= %s"
                params = (start_date,)

            if dry_run:
                sql = f"""
                    WITH ranked AS (
                        SELECT
                            asm.asset_id,
                            DATE(q.quote_time AT TIME ZONE 'UTC') AS market_date,
                            ROW_NUMBER() OVER (
                                PARTITION BY asm.asset_id, DATE(q.quote_time AT TIME ZONE 'UTC')
                                ORDER BY q.quote_time DESC
                            ) AS rn
                        FROM src_cmc.cmc_asset_quote_snapshot q
                        JOIN core.asset_source_map asm
                            ON asm.source_code = 'cmc'
                            AND asm.source_asset_key = q.cmc_id::text
                        {date_filter}
                    )
                    SELECT count(*), min(market_date), max(market_date), count(DISTINCT asset_id)
                    FROM ranked
                    WHERE rn = 1 AND asset_id IS NOT NULL
                """
                cur.execute(sql, params)
                row = cur.fetchone()
                return {
                    "total": row[0],
                    "date_from": str(row[1]) if row[1] else None,
                    "date_to": str(row[2]) if row[2] else None,
                    "assets": row[3],
                    "dry_run": True,
                }

            # 批量 INSERT ... ON CONFLICT
            sql = f"""
                INSERT INTO biz.asset_market_daily
                    (asset_id, market_date, source_code, price_usd,
                     market_cap, fdv, circulating_supply, total_supply,
                     volume_24h, change_24h, change_7d, raw_ref)
                WITH ranked AS (
                    SELECT
                        asm.asset_id,
                        DATE(q.quote_time AT TIME ZONE 'UTC') AS market_date,
                        q.price_usd,
                        q.market_cap,
                        q.fdv,
                        q.circulating_supply,
                        q.total_supply,
                        q.volume_24h,
                        q.percent_change_24h AS change_24h,
                        q.percent_change_7d AS change_7d,
                        q.is_anomaly,
                        ROW_NUMBER() OVER (
                            PARTITION BY asm.asset_id, DATE(q.quote_time AT TIME ZONE 'UTC')
                            ORDER BY q.quote_time DESC
                        ) AS rn
                    FROM src_cmc.cmc_asset_quote_snapshot q
                    JOIN core.asset_source_map asm
                        ON asm.source_code = 'cmc'
                        AND asm.source_asset_key = q.cmc_id::text
                    WHERE (q.is_anomaly IS NOT TRUE OR q.is_anomaly IS NULL)
                    {date_filter}
                )
                SELECT
                    asset_id, market_date, 'cmc', price_usd,
                    market_cap, fdv, circulating_supply, total_supply,
                    volume_24h, change_24h, change_7d,
                    '{{"source": "cmc_quote_snapshot"}}'::jsonb
                FROM ranked
                WHERE rn = 1 AND asset_id IS NOT NULL
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
            """
            cur.execute(sql, params)
            affected = cur.rowcount
            conn.commit()

            # 验证
            cur.execute("""
                SELECT count(*), min(market_date), max(market_date), count(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE source_code = 'cmc'
            """)
            row = cur.fetchone()
            return {
                "affected": affected,
                "total": row[0],
                "date_from": str(row[1]) if row[1] else None,
                "date_to": str(row[2]) if row[2] else None,
                "assets": row[3],
            }


def check_daily_continuity() -> dict:
    """日价连续性自检：检测昨日是否有缺失，若有则告警+自动重试一次。

    RT-BACKTEST-D1-001 改动 3：在 ETL 末尾执行，确保单日失败可重试而非静默缺日。
    """
    from datetime import date, timedelta

    settings = get_settings(require_database=True)
    yesterday = date.today() - timedelta(days=1)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            # 检查昨日 asset_market_daily 覆盖
            cur.execute("""
                SELECT COUNT(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE market_date = %s
            """, (yesterday,))
            row = cur.fetchone()
            asset_count = row[0] if row else 0

            # 对比前日作为基准
            day_before = yesterday - timedelta(days=1)
            cur.execute("""
                SELECT COUNT(DISTINCT asset_id)
                FROM biz.asset_market_daily
                WHERE market_date = %s
            """, (day_before,))
            row2 = cur.fetchone()
            baseline_count = row2[0] if row2 else 0

    result = {
        "check_date": yesterday.isoformat(),
        "asset_count": asset_count,
        "baseline_count": baseline_count,
        "has_gap": asset_count < 100,
        "retry_triggered": False,
    }

    if asset_count < 100 and baseline_count > 100:
        # 昨日缺失，尝试重试 ETL
        print(f"[CONTINUITY] 昨日 {yesterday} 仅 {asset_count} 条记录（基准 {baseline_count}），触发自动重试",
              file=sys.stderr)
        try:
            retry_result = etl_cmc_to_daily(days=2, dry_run=False)
            result["retry_triggered"] = True
            result["retry_result"] = retry_result
            print(f"[CONTINUITY] 重试完成: affected={retry_result.get('affected', 0)}", file=sys.stderr)
        except Exception as e:
            result["retry_error"] = str(e)
            print(f"[CONTINUITY] 重试失败: {e}", file=sys.stderr)
    elif asset_count >= 100:
        print(f"[CONTINUITY] 昨日 {yesterday} 正常: {asset_count} 条记录")
    else:
        print(f"[CONTINUITY] 昨日 {yesterday} 缺失但无基准可对比（前日也缺失）", file=sys.stderr)

    return result


def main() -> int:
    parser = build_parser()
    parser.add_argument(
        "--check-continuity",
        action="store_true",
        help="仅执行昨日连续性自检（不执行主 ETL）",
    )
    args = parser.parse_args()

    if args.check_continuity:
        result = check_daily_continuity()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if not result.get("has_gap") or result.get("retry_triggered") else 1

    print(f"[ETL] asset_market_daily from CMC snapshots")
    if args.days:
        print(f"[ETL] Range: last {args.days} days")
    else:
        print(f"[ETL] Range: all available data")

    result = etl_cmc_to_daily(days=args.days, dry_run=args.dry_run)

    if result.get("dry_run"):
        print(f"[DRY-RUN] Would process {result['total']} rows ({result['assets']} assets)")
        if result.get("date_from"):
            print(f"[DRY-RUN] Date range: {result['date_from']} ~ {result['date_to']}")
    else:
        print(f"[ETL] Affected: {result['affected']:,} rows")
        print(f"[ETL] Total now: {result['total']:,} rows ({result['assets']:,} assets)")
        if result.get("date_from"):
            print(f"[ETL] Date range: {result['date_from']} ~ {result['date_to']}")

    # RT-BACKTEST-D1-001 改动 3：日价连续性自检
    if not args.dry_run:
        print(f"\n[ETL] === 日价连续性自检 ===")
        continuity = check_daily_continuity()
        if continuity.get("has_gap") and not continuity.get("retry_triggered"):
            print(f"[ETL] WARNING: 连续性自检发现缺口且重试未触发", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
