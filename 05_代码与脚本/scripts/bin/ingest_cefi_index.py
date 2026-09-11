"""入库脚本：CEFI 指数日频数据（cryptoETF）。

从 cryptoetf.today API 拉取 CEFI 指数历史，upsert 到 biz.cefi_index_daily。
幂等：同一天重复运行会更新而非重复插入。

用法：
    python ingest_cefi_index.py              # 增量更新
    python ingest_cefi_index.py --full       # 全量回填
    python ingest_cefi_index.py --dry-run    # 预览，不写入
"""

from __future__ import annotations

import argparse
import os
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


CRYPTOETF_BASE = "https://api.cryptoetf.today/api"
TIMEOUT = 30
SOURCE_CODE = "cryptoetf"


def _get_api_key(settings) -> str | None:
    """取 CEFI API Key。"""
    key = os.environ.get("CRYPTOETF_KEY", "")
    if not key:
        key = getattr(settings, "cryptoetf_api_key", None) or ""
    return key or None


def ensure_table(conn) -> None:
    """建表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.cefi_index_daily (
                metric_date    DATE           NOT NULL PRIMARY KEY,
                value          NUMERIC(12,4)  NOT NULL,
                source_code    VARCHAR(20)    NOT NULL DEFAULT 'cryptoetf',
                fetched_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cefi_index_daily_date
                ON biz.cefi_index_daily(metric_date DESC);
        """)
    conn.commit()


def fetch_cefi_history(api_key: str, days: int = 365) -> list[tuple[date, float]]:
    """从 cryptoETF API 拉取 CEFI 指数历史，返回 [(date, value), ...] 按日期升序。"""
    print(f"[cefi_index] fetching CEFI history (days={days}) ...")
    r = requests.get(
        f"{CRYPTOETF_BASE}/v1/index/cefi/history",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"days": days},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    result = []
    for item in data:
        val = item.get("value")
        ts = item.get("date") or item.get("timestamp") or item.get("ts")
        if val is None or ts is None:
            continue
        try:
            val_f = float(val)
        except (ValueError, TypeError):
            continue
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        else:
            ts_str = str(ts)[:10]
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d").date()
            except ValueError:
                continue
        result.append((dt, val_f))
    result.sort(key=lambda x: x[0])
    print(f"[cefi_index] got {len(result)} days, "
          f"range: {result[0][0]} ~ {result[-1][0]}" if result else f"[cefi_index] got 0 days")
    return result


def get_latest_db_date(conn) -> date | None:
    """查库里最新日期。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(metric_date) FROM biz.cefi_index_daily WHERE source_code = %s", (SOURCE_CODE,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_data(conn, data: list[tuple[date, float]], dry_run: bool = False) -> int:
    """upsert 数据，返回写入行数。"""
    if not data:
        return 0
    rows_inserted = 0
    with conn.cursor() as cur:
        for dt, val in data:
            cur.execute("""
                INSERT INTO biz.cefi_index_daily
                    (metric_date, value, source_code, fetched_at, updated_at)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (metric_date) DO UPDATE
                SET value = EXCLUDED.value,
                    source_code = EXCLUDED.source_code,
                    updated_at = NOW()
            """, (dt, val, SOURCE_CODE))
            rows_inserted += 1
    if not dry_run:
        conn.commit()
        print(f"[cefi_index] upserted {rows_inserted} rows")
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="CEFI 指数日频数据采集（cryptoETF）")
    parser.add_argument("--full", action="store_true", help="全量回填（默认增量）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    api_key = _get_api_key(settings)
    if not api_key:
        print("[cefi_index] CRYPTOETF_KEY 未设置，跳过")
        return

    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        latest_db = get_latest_db_date(conn)
        print(f"[cefi_index] latest in DB: {latest_db}")

        # 全量 365 天，增量 180 天
        days = 365 if args.full else 180
        all_data = fetch_cefi_history(api_key, days=days)

        if not args.full and latest_db:
            new_data = [(d, v) for d, v in all_data if d >= latest_db]
            print(f"[cefi_index] incremental mode: {len(new_data)} new/updated days")
        else:
            new_data = all_data
            print(f"[cefi_index] full mode: {len(new_data)} days")

        if args.dry_run:
            print(f"[cefi_index] DRY RUN: would upsert {len(new_data)} rows")
            if new_data:
                print(f"  first: {new_data[0][0]} = {new_data[0][1]:.2f}")
                print(f"  last:  {new_data[-1][0]} = {new_data[-1][1]:.2f}")
            return

        upsert_data(conn, new_data, dry_run=False)
        print("[cefi_index] done ✓")


if __name__ == "__main__":
    main()
