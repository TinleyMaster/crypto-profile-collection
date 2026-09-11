"""入库脚本：BTC 未平仓合约（OI）日频数据（Binance）。

从 Binance Futures API 拉取 BTCUSDT 1d OI 历史，upsert 到 biz.btc_oi_daily。
幂等：同一天重复运行会更新而非重复插入。

用法：
    python ingest_btc_oi.py              # 增量更新
    python ingest_btc_oi.py --full       # 全量回填
    python ingest_btc_oi.py --dry-run    # 预览，不写入
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


BINANCE_FAPI = "https://fapi.binance.com"
TIMEOUT = 30
SOURCE_CODE = "binance"
SYMBOL = "BTCUSDT"


def ensure_table(conn) -> None:
    """建表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.btc_oi_daily (
                metric_date    DATE           NOT NULL PRIMARY KEY,
                open_interest  NUMERIC(24,4) NOT NULL,
                source_code    VARCHAR(20)    NOT NULL DEFAULT 'binance',
                fetched_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_btc_oi_daily_date
                ON biz.btc_oi_daily(metric_date DESC);
        """)
    conn.commit()


def fetch_oi_history(limit: int = 500) -> list[tuple[date, float]]:
    """从 Binance 拉取 BTC 1d OI 历史，返回 [(date, oi), ...] 按日期升序。"""
    print(f"[btc_oi] fetching Binance OI history (limit={limit}) ...")
    r = requests.get(
        f"{BINANCE_FAPI}/futures/data/openInterestHist",
        params={"symbol": SYMBOL, "period": "1d", "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    result = []
    for row in rows:
        ts = row.get("timestamp")
        oi = row.get("sumOpenInterest")
        if ts is None or oi is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts) // 1000, tz=timezone.utc).date()
            oi_val = float(oi)
            result.append((dt, oi_val))
        except (ValueError, TypeError):
            continue
    result.sort(key=lambda x: x[0])
    print(f"[btc_oi] got {len(result)} days, "
          f"range: {result[0][0]} ~ {result[-1][0]}")
    return result


def get_latest_db_date(conn) -> date | None:
    """查库里最新日期。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(metric_date) FROM biz.btc_oi_daily WHERE source_code = %s", (SOURCE_CODE,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_data(conn, data: list[tuple[date, float]], dry_run: bool = False) -> int:
    """upsert 数据，返回写入行数。"""
    if not data:
        return 0
    rows_inserted = 0
    with conn.cursor() as cur:
        for dt, oi in data:
            cur.execute("""
                INSERT INTO biz.btc_oi_daily
                    (metric_date, open_interest, source_code, fetched_at, updated_at)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (metric_date) DO UPDATE
                SET open_interest = EXCLUDED.open_interest,
                    source_code = EXCLUDED.source_code,
                    updated_at = NOW()
            """, (dt, oi, SOURCE_CODE))
            rows_inserted += 1
    if not dry_run:
        conn.commit()
        print(f"[btc_oi] upserted {rows_inserted} rows")
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 未平仓合约日频数据采集（Binance）")
    parser.add_argument("--full", action="store_true", help="全量回填（默认增量）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        latest_db = get_latest_db_date(conn)
        print(f"[btc_oi] latest in DB: {latest_db}")

        # Binance OI 1d 最多 500 条
        all_data = fetch_oi_history(limit=500)

        if not args.full and latest_db:
            new_data = [(d, o) for d, o in all_data if d >= latest_db]
            print(f"[btc_oi] incremental mode: {len(new_data)} new/updated days")
        else:
            new_data = all_data
            print(f"[btc_oi] full mode: {len(new_data)} days")

        if args.dry_run:
            print(f"[btc_oi] DRY RUN: would upsert {len(new_data)} rows")
            if new_data:
                print(f"  first: {new_data[0][0]} = {new_data[0][1]:,.0f} USDT")
                print(f"  last:  {new_data[-1][0]} = {new_data[-1][1]:,.0f} USDT")
            return

        upsert_data(conn, new_data, dry_run=False)
        print("[btc_oi] done ✓")


if __name__ == "__main__":
    main()
