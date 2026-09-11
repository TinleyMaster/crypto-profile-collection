"""入库脚本：赛道 TVL 日频快照（DeFi Llama /protocols 按 category 聚合）。

从 DeFi Llama /protocols 拉全量协议，按 category 聚合 TVL + 7d 变化，upsert 到 biz.category_tvl_daily。
幂等：同一天同 category 重复运行会更新而非重复插入。

用法：
    python ingest_category_tvl.py              # 采集当日快照
    python ingest_category_tvl.py --dry-run    # 预览，不写入
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402


DL_BASE = "https://api.llama.fi"
TIMEOUT = 60  # DeFi Llama /protocols 数据量大，超时放宽
SOURCE_CODE = "defillama"
TVL_CHANGE_CAP = 500  # 剔除天文值（与 macro_market.py 保持一致）


def _safe_float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def ensure_table(conn) -> None:
    """建表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS biz.category_tvl_daily (
                snapshot_date      DATE           NOT NULL,
                category           VARCHAR(100)   NOT NULL,
                tvl_usd            NUMERIC(24,2)  NOT NULL DEFAULT 0,
                tvl_change_7d_pct  NUMERIC(12,4),
                protocol_count     INT            NOT NULL DEFAULT 0,
                source_code        VARCHAR(20)    NOT NULL DEFAULT 'defillama',
                fetched_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                PRIMARY KEY (snapshot_date, category)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_date
                ON biz.category_tvl_daily(snapshot_date DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_category_tvl_daily_cat
                ON biz.category_tvl_daily(category);
        """)
    conn.commit()


def fetch_and_aggregate() -> tuple[date, list[tuple[str, float, float | None, int]]]:
    """从 DeFi Llama 拉全量协议并按 category 聚合。

    返回 (snapshot_date, [(category, tvl, tvl_change_7d_pct, protocol_count), ...])
    """
    print("[category_tvl] fetching DeFi Llama /protocols ...")
    r = requests.get(f"{DL_BASE}/protocols", timeout=TIMEOUT)
    r.raise_for_status()
    prots = r.json()
    print(f"[category_tvl] got {len(prots)} protocols")

    agg: dict[str, dict] = {}
    for p in prots:
        cat = p.get("category") or "Unknown"
        tvl = _safe_float(p.get("tvl"))
        ch7_raw = p.get("change_7d")

        e = agg.setdefault(cat, {
            "tvl": 0.0,
            "wtvl": 0.0,
            "wsum": 0.0,
            "n": 0,
        })
        e["tvl"] += tvl

        if ch7_raw is not None:
            ch7 = _safe_float(ch7_raw)
            if abs(ch7) <= TVL_CHANGE_CAP:
                e["wtvl"] += tvl
                e["wsum"] += ch7 * tvl
                e["n"] += 1

    result = []
    for cat, e in agg.items():
        tvl = e["tvl"]
        change = (e["wsum"] / e["wtvl"]) if e["wtvl"] > 0 else None
        result.append((cat, tvl, change, e["n"]))

    result.sort(key=lambda x: -x[1])  # 按 TVL 降序
    today = datetime.now(tz=timezone.utc).date()
    print(f"[category_tvl] aggregated into {len(result)} categories, "
          f"top: {result[0][0]}=${result[0][1]/1e9:.1f}B")
    return today, result


def get_latest_db_date(conn) -> date | None:
    """查库里最新快照日期。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_date) FROM biz.category_tvl_daily WHERE source_code = %s", (SOURCE_CODE,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def upsert_data(conn, snapshot_date: date, data: list[tuple[str, float, float | None, int]],
                dry_run: bool = False) -> int:
    """upsert 数据，返回写入行数。"""
    if not data:
        return 0
    rows_inserted = 0
    with conn.cursor() as cur:
        for cat, tvl, ch7, n in data:
            cur.execute("""
                INSERT INTO biz.category_tvl_daily
                    (snapshot_date, category, tvl_usd, tvl_change_7d_pct, protocol_count,
                     source_code, fetched_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (snapshot_date, category) DO UPDATE
                SET tvl_usd = EXCLUDED.tvl_usd,
                    tvl_change_7d_pct = EXCLUDED.tvl_change_7d_pct,
                    protocol_count = EXCLUDED.protocol_count,
                    source_code = EXCLUDED.source_code,
                    updated_at = NOW()
            """, (snapshot_date, cat, tvl, ch7, n, SOURCE_CODE))
            rows_inserted += 1
    if not dry_run:
        conn.commit()
        print(f"[category_tvl] upserted {rows_inserted} rows for {snapshot_date}")
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="赛道 TVL 日频快照采集（DeFi Llama）")
    parser.add_argument("--dry-run", action="store_true", help="预览，不写入数据库")
    parser.add_argument("--force", action="store_true", help="即使今天已有数据也强制刷新")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        ensure_table(conn)

        latest_db = get_latest_db_date(conn)
        today = datetime.now(tz=timezone.utc).date()
        print(f"[category_tvl] latest in DB: {latest_db}, today: {today}")

        if not args.force and latest_db == today:
            print(f"[category_tvl] today's data already exists, skipping (use --force to refresh)")
            return

        snapshot_date, data = fetch_and_aggregate()

        if args.dry_run:
            print(f"[category_tvl] DRY RUN: would upsert {len(data)} rows for {snapshot_date}")
            for cat, tvl, ch7, n in data[:10]:
                ch7_str = f"{ch7:+.2f}%" if ch7 is not None else "N/A"
                print(f"  {cat:<30s} TVL=${tvl/1e9:>8.2f}B  7d={ch7_str:<10s}  n={n}")
            if len(data) > 10:
                print(f"  ... and {len(data) - 10} more")
            return

        upsert_data(conn, snapshot_date, data, dry_run=False)
        print("[category_tvl] done ✓")


if __name__ == "__main__":
    main()
