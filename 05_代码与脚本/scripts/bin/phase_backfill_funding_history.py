#!/usr/bin/env python3
"""盘面异动扫描 · funding 历史回填 + 增量采集 → biz.funding_rate_hist。

Binance /fapi/v1/fundingRate 免费提供约 333 天 8h 结算历史（每请求 1000 条）。
用途：回测 funding 消融（设计方案 §8 局限项 4）+ 实时拥挤度标签的历史序列。

用法：
    python phase_backfill_funding_history.py              # 全量回填（宇宙=有 1h 历史的符号）
    python phase_backfill_funding_history.py --incremental # 增量：只补最新缺失的结算点
    python phase_backfill_funding_history.py --limit-symbols 5 --dry-run   # 冒烟
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
TIMEOUT = 20
MAX_RETRIES = 3
MIN_REQUEST_GAP = 0.3          # ≈3.3 req/s，保守防限频
FUNDING_LIMIT = 1000           # fundingRate 单请求上限
MIN_KLINES_BARS = 1000         # 只回填有足够 1h 历史的符号（回测宇宙）
COVERED_COUNT = 900            # 已有 >=900 条结算记录则视为已覆盖

_SESSION = requests.Session()
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0

UPSERT_SQL = """
    INSERT INTO biz.funding_rate_hist (symbol, funding_time, rate, source_code, fetched_at)
    VALUES (%s,%s,%s,'binance',NOW())
    ON CONFLICT (symbol, funding_time) DO UPDATE SET
        rate=EXCLUDED.rate, fetched_at=NOW()
"""


def _get(url: str, params: dict) -> list:
    global _LAST_REQUEST_TS
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 2):
        try:
            with _REQUEST_LOCK:
                gap = MIN_REQUEST_GAP - (time.time() - _LAST_REQUEST_TS)
                if gap > 0:
                    time.sleep(gap)
                r = _SESSION.get(url, params=params, timeout=TIMEOUT)
                _LAST_REQUEST_TS = time.time()
            if r.status_code == 429:
                time.sleep(min(5 * (2 ** attempt), 60))
                continue
            if r.status_code == 418:
                time.sleep(60 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < MAX_RETRIES + 1:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


def get_universe(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM biz.asset_klines WHERE interval='1h' "
            "GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol",
            (MIN_KLINES_BARS,),
        )
        return [r[0] for r in cur.fetchall()]


def get_covered(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, COUNT(*) AS n FROM biz.funding_rate_hist GROUP BY symbol"
        )
        return {sym for sym, n in cur.fetchall() if n >= COVERED_COUNT}


def get_last_funding_time(conn, symbol: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(funding_time) AS t FROM biz.funding_rate_hist WHERE symbol=%s",
            (symbol,),
        )
        r = cur.fetchone()
        return r[0] if r else None


def fetch_funding(symbol: str, incremental: bool, last_time: datetime | None) -> list[tuple]:
    """返回 [(funding_time, rate), ...] 升序。"""
    params: dict = {"symbol": symbol, "limit": FUNDING_LIMIT}
    if incremental and last_time is not None:
        params["startTime"] = int(last_time.timestamp() * 1000) + 1
    data = _get(f"{FAPI_BASE}/fapi/v1/fundingRate", params)
    return [(datetime.fromtimestamp(d["fundingTime"] / 1000.0, tz=timezone.utc),
             float(d["fundingRate"])) for d in data]


def main() -> int:
    parser = argparse.ArgumentParser(description="funding 历史回填/增量 → biz.funding_rate_hist")
    parser.add_argument("--incremental", action="store_true", help="增量模式（默认全量回填）")
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        symbols = get_universe(conn)
        covered = get_covered(conn) if not args.incremental else set()
        todo = [s for s in symbols if s not in covered]
        if args.limit_symbols:
            todo = todo[: args.limit_symbols]
        print(f"[funding] {'增量' if args.incremental else '回填'} 宇宙 {len(symbols)}，"
              f"跳过 {len(covered)}，待处理 {len(todo)}")

        results: dict[str, list[tuple]] = {}
        errors = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            def _run(sym):
                last = get_last_funding_time(conn, sym) if args.incremental else None
                return sym, fetch_funding(sym, args.incremental, last)
            futs = {pool.submit(_run, s): s for s in todo}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    _, rows = fut.result()
                    results[s] = rows
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 10:
                        print(f"[warn] {s} 失败: {e}", file=sys.stderr)

        total = sum(len(v) for v in results.values())
        print(f"[funding] 完成 {len(results)} 符号，{total} 条，失败 {errors}")
        if args.dry_run:
            for s, rows in list(results.items())[:3]:
                print(f"  {s}: {len(rows)} 条，{rows[0][0].isoformat() if rows else '-'} ~ "
                      f"{rows[-1][0].isoformat() if rows else '-'}")
            return 0
        if not results:
            return 0
        all_rows = [(s, t, r) for s, rows in results.items() for t, r in rows]
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, all_rows)
        print(f"[db] upsert {len(all_rows)} 条 funding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
