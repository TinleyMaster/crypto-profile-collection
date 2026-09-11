"""入库脚本：恐贪指数日频数据（CMC）。

从 CoinMarketCap trial API 拉取恐贪指数历史，upsert 到 biz.fear_greed_daily。
幂等：同一天重复运行会更新而非重复插入。

用法：
    python ingest_fear_greed.py              # 增量更新（补齐缺失日期）
    python ingest_fear_greed.py --full       # 全量回填
    python ingest_fear_greed.py --dry-run    # 预览，不写入
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


CMC_BASE = "https://pro-api.coinmarketcap.com"
TIMEOUT = 30
SOURCE_CODE = "cmc"


def _get_api_key(settings) -> str | None:
    """从 settings 或环境变量取 CMC API Key。"""
    key = getattr(settings, "cmc_api_key", None)
    if not key:
        import os
        key = os.environ.get("CMC_API_KEY") or os.environ.get("COINMARKETCAP_API_KEY")
    return key


def ensure_table(conn) -> None:
    """建表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.fear_greed_daily (
                metric_date    DATE        NOT NULL PRIMARY KEY,
                value          INT         NOT NULL,
                value_class    VARCHAR(20),
                source_code    VARCHAR(20) NOT NULL DEFAULT 'cmc',
                fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_date
                ON biz.fear_greed_daily(metric_date DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_value
                ON biz.fear_greed_daily(value);
        """)
    conn.commit()


def fetch_fear_greed_history(settings, days: int = 365 * 3) -> list[tuple[date, int, str | None]]:
    """从 CMC 拉取恐贪指数历史，返回 [(date, value, value_class), ...] 按日期升序。"""
    api_key = _get_api_key(settings)
    headers = {}
    params = {"limit": days}
    if api_key:
        headers["X-CMC_PRO_API_KEY"] = api_key
        url = f"{CMC_BASE}/v3/fear-and-greed/historical"
    else:
        # 没有 API Key 时用 trial API
        url = f"{CMC_BASE}/trial-pro-api/v3/fear-and-greed"

    print(f"[fear_greed] fetching {url} (days={days}) ...")
    r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data", [])
    result = []
    for item in data:
        val = item.get("value")
        ts = item.get("timestamp") or item.get("date")
        if val is None or ts is None:
            continue
        try:
            val_int = int(float(val))
        except (ValueError, TypeError):
            continue
        # timestamp 可能是字符串或 unix 秒
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        else:
            # 字符串格式，取前 10 位日期
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y"):
                try:
                    dt = datetime.strptime(str(ts)[:10] if fmt == "%Y-%m-%d" else str(ts), fmt).date()
                    break
                except ValueError:
                    continue
            else:
                try:
                    dt = datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
        vclass = item.get("value_classification") or item.get("classification")
        result.append((dt, val_int, vclass))
    result.sort(key=lambda x: x[0])
    print(f"[fear_greed] got {len(result)} days, "
          f"range: {result[0][0]} ~ {result[-1][0]}")
    return result


def get_latest_db_date(conn) -> date | None:
    """查库里最新日期。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(metric_date) FROM biz.fear_greed_daily WHERE source_code = %s", (SOURCE_CODE,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_data(conn, data: list[tuple[date, int, str | None]], dry_run: bool = False) -> int:
    """upsert 数据，返回写入行数。"""
    if not data:
        return 0
    rows_inserted = 0
    with conn.cursor() as cur:
        for dt, value, vclass in data:
            cur.execute("""
                INSERT INTO biz.fear_greed_daily
                    (metric_date, value, value_class, source_code, fetched_at, updated_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (metric_date) DO UPDATE
                SET value = EXCLUDED.value,
                    value_class = EXCLUDED.value_class,
                    source_code = EXCLUDED.source_code,
                    updated_at = NOW()
            """, (dt, value, vclass, SOURCE_CODE))
            rows_inserted += 1
    if not dry_run:
        conn.commit()
        print(f"[fear_greed] upserted {rows_inserted} rows")
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="恐贪指数日频数据采集（CMC）")
    parser.add_argument("--full", action="store_true", help="全量回填（默认增量）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        latest_db = get_latest_db_date(conn)
        print(f"[fear_greed] latest in DB: {latest_db}")

        # 拉取：全量模式拉 3 年，增量拉 180 天（足够补齐缺口）
        days = 365 * 3 if args.full else 180
        all_data = fetch_fear_greed_history(settings, days=days)

        if not args.full and latest_db:
            new_data = [(d, v, c) for d, v, c in all_data if d >= latest_db]
            print(f"[fear_greed] incremental mode: {len(new_data)} new/updated days")
        else:
            new_data = all_data
            print(f"[fear_greed] full mode: {len(new_data)} days")

        if args.dry_run:
            print(f"[fear_greed] DRY RUN: would upsert {len(new_data)} rows")
            if new_data:
                print(f"  first: {new_data[0][0]} = {new_data[0][1]} ({new_data[0][2]})")
                print(f"  last:  {new_data[-1][0]} = {new_data[-1][1]} ({new_data[-1][2]})")
            return

        upsert_data(conn, new_data, dry_run=False)
        print("[fear_greed] done ✓")


if __name__ == "__main__":
    main()
