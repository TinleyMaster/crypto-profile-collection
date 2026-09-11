"""入库脚本：稳定币总供给日频数据（DeFi Llama）。

从 stablecoins.llama.fi 拉取全市场稳定币总供给历史，upsert 到 biz.stablecoin_supply_daily。
同时计算每日净流入（当日供给 - 前日供给）。
幂等：同一天重复运行会更新而非重复插入。

用法：
    python ingest_stablecoin_supply.py              # 增量更新（补齐缺失日期）
    python ingest_stablecoin_supply.py --full       # 全量回填（覆盖全部历史）
    python ingest_stablecoin_supply.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


API_URL = "https://stablecoins.llama.fi/stablecoincharts/All"
TIMEOUT = 30
SOURCE_CODE = "defillama"


def ensure_table(conn) -> None:
    """建表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.stablecoin_supply_daily (
                metric_date       DATE        NOT NULL,
                total_supply_usd  NUMERIC(24,2),
                net_flow_usd      NUMERIC(20,2),
                source_code       TEXT        NOT NULL DEFAULT 'defillama',
                fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (metric_date, source_code)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_stablecoin_supply_daily_date
                ON biz.stablecoin_supply_daily (metric_date DESC);
        """)
    conn.commit()


def fetch_supply_history() -> list[tuple[date, float]]:
    """从 DeFi Llama 拉取稳定币总供给历史，返回 [(date, supply_usd), ...] 按日期升序。"""
    print(f"[stablecoin] fetching {API_URL} ...")
    r = requests.get(API_URL, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    result = []
    for row in rows:
        usd = (row.get("totalCirculating") or {}).get("peggedUSD")
        ts = row.get("date")
        if usd is None or ts is None:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        result.append((dt, float(usd)))
    result.sort(key=lambda x: x[0])
    print(f"[stablecoin] got {len(result)} days of data, "
          f"range: {result[0][0]} ~ {result[-1][0]}")
    return result


def get_latest_db_date(conn) -> date | None:
    """查库里最新日期。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(metric_date) FROM biz.stablecoin_supply_daily
            WHERE source_code = %s
        """, (SOURCE_CODE,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_data(conn, data: list[tuple[date, float]], dry_run: bool = False) -> int:
    """upsert 数据，同时计算 net_flow_usd。返回写入行数。"""
    if not data:
        return 0

    # 按日期升序排好
    data_sorted = sorted(data, key=lambda x: x[0])

    # 计算每个日期的净流入（当日 - 前日）
    # 注意：前日数据可能在传入 data 里，也可能已经在 DB 里
    # 最简单的做法：全部 upsert supply，然后用 SQL 窗口函数统一更新 net_flow

    rows_inserted = 0
    with conn.cursor() as cur:
        for dt, supply in data_sorted:
            cur.execute("""
                INSERT INTO biz.stablecoin_supply_daily
                    (metric_date, total_supply_usd, source_code, fetched_at, updated_at)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (metric_date, source_code) DO UPDATE
                SET total_supply_usd = EXCLUDED.total_supply_usd,
                    updated_at = NOW()
            """, (dt, supply, SOURCE_CODE))
            rows_inserted += 1

    if not dry_run:
        conn.commit()

    # 用 LAG 窗口函数回填 net_flow_usd
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE biz.stablecoin_supply_daily s
            SET net_flow_usd = s.total_supply_usd - prev.supply
            FROM (
                SELECT metric_date, source_code,
                       LAG(total_supply_usd) OVER (
                           PARTITION BY source_code ORDER BY metric_date
                       ) AS supply
                FROM biz.stablecoin_supply_daily
            ) prev
            WHERE s.metric_date = prev.metric_date
              AND s.source_code = prev.source_code
              AND prev.supply IS NOT NULL
              AND s.source_code = %s
        """, (SOURCE_CODE,))
        rows_updated = cur.rowcount

    if not dry_run:
        conn.commit()
        print(f"[stablecoin] upserted {rows_inserted} rows, updated net_flow for {rows_updated} rows")

    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="稳定币总供给日频数据采集（DeFi Llama）")
    parser.add_argument("--full", action="store_true", help="全量回填（默认增量，只更新缺的日期）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        latest_db = get_latest_db_date(conn)
        print(f"[stablecoin] latest in DB: {latest_db}")

        # 拉取全量历史（API 一次返回全部，也不贵）
        all_data = fetch_supply_history()

        if not args.full and latest_db:
            # 增量：只取 DB 里没有的日期 + 最新一天（可能有更新）
            new_data = [(d, s) for d, s in all_data if d >= latest_db]
            print(f"[stablecoin] incremental mode: {len(new_data)} new/updated days")
        else:
            new_data = all_data
            print(f"[stablecoin] full mode: {len(new_data)} days")

        if args.dry_run:
            print(f"[stablecoin] DRY RUN: would upsert {len(new_data)} rows")
            if new_data:
                print(f"  first: {new_data[0][0]} = {new_data[0][1]:,.0f} USD")
                print(f"  last:  {new_data[-1][0]} = {new_data[-1][1]:,.0f} USD")
            return

        upsert_data(conn, new_data, dry_run=False)
        print("[stablecoin] done ✓")


if __name__ == "__main__":
    main()
