"""入库脚本：BTC 资金费率日频数据（Binance）。

从 Binance Futures /fapi/v1/fundingRate 拉取 BTCUSDT 历史资金费率（8h 结算一次），
按天聚合取当天最后一条，upsert 到 biz.btc_funding_daily。
幂等：同一天重复运行会更新而非重复插入。

用法：
    python ingest_btc_funding.py              # 增量更新
    python ingest_btc_funding.py --full       # 全量回填（分页拉历史）
    python ingest_btc_funding.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


BINANCE_FAPI = "https://fapi.binance.com"
TIMEOUT = 30
SOURCE_CODE = "binance"
SYMBOL = "BTCUSDT"
# 每页最大 1000 条，8h 一条 ≈ 333 天；从 2025-01-01 起拉足够覆盖全部快照历史
DEFAULT_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
# Binance fundingRate 端点实际单页上限为 500 条（limit=1000 会被截断），
# 分页按 500 条窗口推进，避免返回不足触发提前 break
PAGE_LIMIT = 500


def ensure_table(conn) -> None:
    """建表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.btc_funding_daily (
                metric_date    DATE           NOT NULL PRIMARY KEY,
                funding_rate   NUMERIC(12,8) NOT NULL,
                source_code    VARCHAR(20)    NOT NULL DEFAULT 'binance',
                fetched_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_btc_funding_daily_date
                ON biz.btc_funding_daily(metric_date DESC);
        """)
    conn.commit()


def fetch_funding_page(start_ms: int, end_ms: int, limit: int = PAGE_LIMIT) -> list[tuple[date, float]]:
    """拉一页 funding rate，返回 [(date, rate), ...]（按时间升序）。"""
    r = requests.get(
        f"{BINANCE_FAPI}/fapi/v1/fundingRate",
        params={"symbol": SYMBOL, "startTime": start_ms, "endTime": end_ms, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for row in r.json():
        ts = row.get("fundingTime")
        rate = row.get("fundingRate")
        if ts is None or rate is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts) // 1000, tz=timezone.utc).date()
            out.append((dt, float(rate)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def fetch_funding_history(start_dt: datetime = DEFAULT_START) -> list[tuple[date, float]]:
    """分页拉取 funding rate 历史，按天聚合取当天最后一条。"""
    print(f"[btc_funding] fetching {SYMBOL} funding history from {start_dt.date()} ...")
    all_rows: dict[date, float] = {}
    cursor = start_dt
    end_now = datetime.now(tz=timezone.utc)
    page = 0
    while cursor < end_now:
        start_ms = int(cursor.timestamp() * 1000)
        end_ms = min(int(end_now.timestamp() * 1000), start_ms + (PAGE_LIMIT - 1) * 8 * 3600 * 1000)
        rows = fetch_funding_page(start_ms, end_ms)
        for d, r in rows:
            all_rows[d] = r  # 覆盖为最新一条（同一天取最后）
        page += 1
        if len(rows) < PAGE_LIMIT:
            break
        # 推进 cursor 到该页最后时间
        last_ts = max(rows, key=lambda x: x[0])[0]
        cursor = datetime(last_ts.year, last_ts.month, last_ts.day, tzinfo=timezone.utc) + timedelta(days=1)
        if page > 100:
            print(f"[btc_funding] 分页超过 100 次，提前停止")
            break
        print(f"[btc_funding]  page {page}: {len(all_rows)} days so far")

    result = sorted(all_rows.items(), key=lambda x: x[0])
    print(f"[btc_funding] got {len(result)} days, "
          f"range: {result[0][0]} ~ {result[-1][0]}")
    return result


def get_latest_db_date(conn) -> date | None:
    """查库里最新日期。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(metric_date) FROM biz.btc_funding_daily WHERE source_code = %s", (SOURCE_CODE,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_data(conn, data: list[tuple[date, float]], dry_run: bool = False) -> int:
    """批量 upsert 数据。"""
    if not data:
        return 0
    rows_inserted = 0
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO biz.btc_funding_daily
                (metric_date, funding_rate, source_code, fetched_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (metric_date) DO UPDATE
            SET funding_rate = EXCLUDED.funding_rate,
                source_code = EXCLUDED.source_code,
                updated_at = NOW()
        """, [(dt, r, SOURCE_CODE) for dt, r in data])
        rows_inserted = len(data)
    if not dry_run:
        conn.commit()
        print(f"[btc_funding] upserted {rows_inserted} rows")
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 资金费率日频数据采集（Binance）")
    parser.add_argument("--full", action="store_true", help="全量回填（默认增量）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        latest_db = get_latest_db_date(conn)
        print(f"[btc_funding] latest in DB: {latest_db}")

        all_data = fetch_funding_history()

        if not args.full and latest_db:
            new_data = [(d, r) for d, r in all_data if d >= latest_db]
            print(f"[btc_funding] incremental mode: {len(new_data)} new/updated days")
        else:
            new_data = all_data
            print(f"[btc_funding] full mode: {len(new_data)} days")

        if args.dry_run:
            print(f"[btc_funding] DRY RUN: would upsert {len(new_data)} rows")
            if new_data:
                print(f"  first: {new_data[0][0]} = {new_data[0][1]:.8f}")
                print(f"  last:  {new_data[-1][0]} = {new_data[-1][1]:.8f}")
            return

        upsert_data(conn, new_data, dry_run=False)
        print("[btc_funding] done ✓")


if __name__ == "__main__":
    main()
