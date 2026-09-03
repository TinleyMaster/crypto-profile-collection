"""日价缺口回填脚本（RT-BACKTEST-D1-001 改动 2）

回填 biz.asset_market_daily 中指定日期的缺失日价。

数据源优先级：
1. CMC 历史行情 API（source_code='cmc_historical'）- 首选，覆盖面广
2. 从已有快照重新 ETL（若 cmc_asset_quote_snapshot 有数据但 asset_market_daily 缺失）
3. Binance klines 历史收盘（仅 top 资产兜底）

用法：
    python backfill_daily_gap.py --dates 2026-08-30 2026-08-31    # 回填指定日期
    python backfill_daily_gap.py --days 3                         # 回填最近 3 天的缺口
    python backfill_daily_gap.py --dates 2026-08-30 --dry-run    # 预览，不写入
    python backfill_daily_gap.py --dates 2026-08-30 --verify     # 回填后自动校验
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

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


def ensure_source_platform(conn) -> None:
    """确保 source_code 外键存在。"""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sys.source_platform (platform_code, platform_name, base_url, description, is_active)
            VALUES ('cmc_historical', 'CoinMarketCap Historical Quotes', 'https://coinmarketcap.com', 'CMC 专业版历史行情 API 回填', TRUE)
            ON CONFLICT (platform_code) DO NOTHING
        """)


def check_existing_coverage(conn, target_dates: list[date]) -> dict[date, int]:
    """检查目标日期已有的资产覆盖数。"""
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(target_dates))
        cur.execute(f"""
            SELECT market_date, COUNT(DISTINCT asset_id)
            FROM biz.asset_market_daily
            WHERE market_date IN ({placeholders})
            GROUP BY market_date
        """, target_dates)
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_assets_for_backfill(conn, top_n: int = 8000) -> list[tuple[int, int]]:
    """获取需要回填的资产列表，返回 [(asset_id, cmc_id), ...]。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT asm.asset_id, asm.source_asset_key::INT AS cmc_id
            FROM core.asset_source_map asm
            WHERE asm.source_code = 'cmc'
              AND asm.source_asset_key IS NOT NULL
              AND asm.source_asset_key ~ '^[0-9]+$'
            ORDER BY asm.asset_id
            LIMIT %s
        """, (top_n,))
        return [(row[0], row[1]) for row in cur.fetchall()]


def backfill_via_cmc_historical(
    conn,
    target_dates: list[date],
    assets: list[tuple[int, int]],
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict:
    """通过 CMC 历史行情 API 回填指定日期。"""
    from crypto_research.clients.cmc_client import CMCClient

    settings = get_settings(require_database=True)
    cmc = CMCClient(settings)
    ensure_source_platform(conn)

    # CMC API 按时间范围拉取，取覆盖目标日期的最小范围
    min_date = min(target_dates)
    max_date = max(target_dates)
    time_start = min_date.isoformat()
    time_end = (max_date + timedelta(days=1)).isoformat()  # API end 是 exclusive

    asset_id_map = {cmc_id: aid for aid, cmc_id in assets}
    batch_size = min(batch_size, 100)
    total_batches = (len(assets) + batch_size - 1) // batch_size
    total_rows = 0
    errors = []

    for batch_idx in range(total_batches):
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + batch_size, len(assets))
        batch_cmc_ids = [cmc_id for _, cmc_id in assets[batch_start:batch_end]]

        print(f"[CMC] Batch {batch_idx + 1}/{total_batches}: {len(batch_cmc_ids)} assets")

        try:
            resp = cmc.get_quotes_historical(
                ids=batch_cmc_ids,
                time_start=time_start,
                time_end=time_end,
                interval="daily",
            )
        except Exception as e:
            err_msg = f"Batch {batch_idx + 1} API error: {e}"
            print(f"[CMC] {err_msg}", file=sys.stderr)
            errors.append(err_msg)
            time.sleep(2)
            continue

        # 解析响应
        data = resp.get("data") or {}
        rows = []
        for cmc_id_str, coin_data in data.items():
            cmc_id = int(cmc_id_str)
            asset_id = asset_id_map.get(cmc_id)
            if asset_id is None:
                continue

            for quote_entry in (coin_data.get("quotes") or []):
                timestamp_str = quote_entry.get("timestamp")
                if not timestamp_str:
                    continue
                try:
                    quote_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                market_date = quote_time.date()
                if market_date not in target_dates:
                    continue

                quote_usd = (quote_entry.get("quote") or {}).get("USD") or {}
                rows.append({
                    "asset_id": asset_id,
                    "market_date": market_date,
                    "source_code": "cmc_historical",
                    "price_usd": quote_usd.get("price"),
                    "market_cap": quote_usd.get("market_cap"),
                    "fdv": quote_usd.get("fully_diluted_market_cap"),
                    "circulating_supply": quote_usd.get("circulating_supply"),
                    "total_supply": quote_usd.get("total_supply"),
                    "volume_24h": quote_usd.get("volume_24h"),
                    "change_24h": quote_usd.get("percent_change_24h"),
                    "change_7d": quote_usd.get("percent_change_7d"),
                })

        if rows and not dry_run:
            sql = """
                INSERT INTO biz.asset_market_daily
                    (asset_id, market_date, source_code, price_usd,
                     market_cap, fdv, circulating_supply, total_supply,
                     volume_24h, change_24h, change_7d, raw_ref)
                VALUES (
                    %(asset_id)s, %(market_date)s, %(source_code)s, %(price_usd)s,
                    %(market_cap)s, %(fdv)s, %(circulating_supply)s, %(total_supply)s,
                    %(volume_24h)s, %(change_24h)s, %(change_7d)s,
                    '{"source": "cmc_historical_backfill"}'::jsonb
                )
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
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()
            total_rows += len(rows)
            print(f"[CMC]   Inserted {len(rows)} rows")
        elif dry_run:
            total_rows += len(rows)
            print(f"[CMC]   Would insert {len(rows)} rows (dry-run)")
        else:
            print(f"[CMC]   No rows for target dates")

        time.sleep(1.5)  # 限速

    return {
        "total_rows": total_rows,
        "errors": errors,
        "batches": total_batches,
    }


def re_etl_from_snapshots(conn, target_dates: list[date], dry_run: bool = False) -> dict:
    """从已有快照重新 ETL 到 asset_market_daily（兜底方案）。"""
    with conn.cursor() as cur:
        # 检查快照是否存在
        placeholders = ",".join(["%s"] * len(target_dates))
        cur.execute(f"""
            SELECT DATE(quote_time AT TIME ZONE 'UTC') AS snap_date, COUNT(*) AS cnt
            FROM src_cmc.cmc_asset_quote_snapshot
            WHERE DATE(quote_time AT TIME ZONE 'UTC') IN ({placeholders})
            GROUP BY snap_date
        """, target_dates)
        snap_dates = {row[0]: row[1] for row in cur.fetchall()}

        if not snap_dates:
            return {"skipped": True, "reason": "No snapshots found for target dates"}

        if dry_run:
            return {"dry_run": True, "snap_dates": {k.isoformat(): v for k, v in snap_dates.items()}}

        # 对有快照的日期执行 ETL
        affected = 0
        for snap_date, snap_count in snap_dates.items():
            cur.execute("""
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
                    WHERE DATE(q.quote_time AT TIME ZONE 'UTC') = %s
                      AND (q.is_anomaly IS NOT TRUE OR q.is_anomaly IS NULL)
                )
                SELECT
                    asset_id, market_date, 'cmc', price_usd,
                    market_cap, fdv, circulating_supply, total_supply,
                    volume_24h, change_24h, change_7d,
                    '{"source": "re_etl_backfill"}'::jsonb
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
            """, (snap_date,))
            affected += cur.rowcount
            print(f"[ETL] Re-ETL {snap_date}: {cur.rowcount} rows")

        conn.commit()
        return {"affected": affected, "snap_dates_processed": len(snap_dates)}


def verify_backfill(conn, target_dates: list[date]) -> dict:
    """校验回填结果。"""
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(target_dates))
        cur.execute(f"""
            SELECT market_date, COUNT(DISTINCT asset_id) AS assets, COUNT(*) AS rows,
                   MIN(price_usd) AS min_price, MAX(price_usd) AS max_price
            FROM biz.asset_market_daily
            WHERE market_date IN ({placeholders})
            GROUP BY market_date
            ORDER BY market_date
        """, target_dates)
        stats = {}
        for row in cur.fetchall():
            stats[row[0].isoformat()] = {
                "assets": row[1],
                "rows": row[2],
                "min_price": float(row[3]) if row[3] else None,
                "max_price": float(row[4]) if row[4] else None,
            }

        # 对比相邻日期
        cur.execute("""
            SELECT market_date, COUNT(DISTINCT asset_id) AS assets
            FROM biz.asset_market_daily
            WHERE market_date >= %s - INTERVAL '7 days'
              AND market_date <= %s + INTERVAL '7 days'
            GROUP BY market_date
            ORDER BY market_date
        """, (min(target_dates), max(target_dates)))
        context = {row[0].isoformat(): row[1] for row in cur.fetchall()}

    return {
        "backfilled_dates": stats,
        "context_7d": context,
        "pass": all(
            s["assets"] >= 7000
            for s in stats.values()
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="日价缺口回填")
    parser.add_argument("--dates", nargs="+", help="指定回填日期（YYYY-MM-DD）")
    parser.add_argument("--days", type=int, default=3, help="回填最近 N 天的缺口（默认3）")
    parser.add_argument("--top", type=int, default=8000, help="回填资产数量上限（默认8000）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入")
    parser.add_argument("--verify", action="store_true", help="回填后自动校验")
    parser.add_argument("--skip-cmc", action="store_true", help="跳过 CMC 历史 API，仅 re-ETL")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if args.dates:
        target_dates = [date.fromisoformat(d) for d in args.dates]
    else:
        today = date.today()
        target_dates = [today - timedelta(days=i) for i in range(args.days, 0, -1)]

    print(f"[BACKFILL] Target dates: {[d.isoformat() for d in target_dates]}")
    print(f"[BACKFILL] Mode: {'dry-run' if args.dry_run else 'live'}")

    with get_connection(settings.database_url) as conn:
        # 检查现有覆盖
        existing = check_existing_coverage(conn, target_dates)
        print(f"[BACKFILL] Existing coverage: {existing}")

        missing_dates = [d for d in target_dates if existing.get(d, 0) < 100]
        if not missing_dates:
            print("[BACKFILL] All target dates already have sufficient coverage. Nothing to do.")
            return 0

        print(f"[BACKFILL] Missing dates (need backfill): {[d.isoformat() for d in missing_dates]}")

        # 方案1: CMC 历史 API
        if not args.skip_cmc:
            assets = fetch_assets_for_backfill(conn, args.top)
            print(f"[BACKFILL] Assets for CMC backfill: {len(assets)}")

            cmc_result = backfill_via_cmc_historical(
                conn, missing_dates, assets, dry_run=args.dry_run
            )
            print(f"[BACKFILL] CMC result: {cmc_result}")

        # 方案2: re-ETL from snapshots（兜底）
        etl_result = re_etl_from_snapshots(conn, missing_dates, dry_run=args.dry_run)
        print(f"[BACKFILL] Re-ETL result: {etl_result}")

    # 校验
    if args.verify and not args.dry_run:
        with get_connection(settings.database_url) as conn:
            verify_result = verify_backfill(conn, target_dates)
            print(f"[VERIFY] {json.dumps(verify_result, ensure_ascii=False, indent=2, default=str)}")
            if not verify_result["pass"]:
                print("[VERIFY] WARNING: Some dates still have low coverage!", file=sys.stderr)
                return 1

    print("[BACKFILL] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
