#!/usr/bin/env python3
"""盘面异动扫描 P1 · OI 历史回填：Binance openInterestHist(1h) → biz.oi_cvd_snapshot。

回测前提数据：OI/CVD 采样器自 5m 起持续积累，但历史缺失；
openInterestHist（weight=0，独立桶 1000 req/5min）免费提供最近 ~30 天 1h OI，
回填后与实时 5m 桶在同一表共存（date_trunc('hour') 聚合即得小时序列）。

只回填 asset_klines 中有 1h 历史（>=1000 根）的符号（即回测候选宇宙），
带全局限频 + 418/429 退避 + 断点续跑（已有 >=25 天历史则跳过）。

用法：
    python phase_backfill_oi_history.py                 # 全量回填
    python phase_backfill_oi_history.py --limit-symbols 5 --dry-run   # 冒烟
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
PAGE_LIMIT = 500               # openInterestHist 单请求上限
MIN_KLINES_BARS = 1000         # 只回填有足够 1h K 线历史的符号
COVERED_DAYS = 25              # 已有 >=25 天 OI 历史则跳过

UPSERT_SQL = """
    INSERT INTO biz.oi_cvd_snapshot (symbol, ts, exchange, oi_usd, cvd_5m_usd, cvd_1h_usd, vol_5m_usd)
    VALUES (%s,%s,'binance',%s,NULL,NULL,NULL)
    ON CONFLICT (symbol, exchange, ts) DO UPDATE SET oi_usd=EXCLUDED.oi_usd
"""


def get_universe(conn) -> list[str]:
    """asset_klines 中有 >=MIN_KLINES_BARS 根 1h K 线的符号（回测候选宇宙）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, COUNT(*) AS n FROM biz.asset_klines "
            "WHERE interval='1h' GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol",
            (MIN_KLINES_BARS,),
        )
        return [r[0] for r in cur.fetchall()]


def get_covered(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, MIN(ts) AS min_ts FROM biz.oi_cvd_snapshot "
            "WHERE oi_usd IS NOT NULL GROUP BY symbol"
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=COVERED_DAYS)
        return {sym for sym, min_ts in cur.fetchall() if min_ts and min_ts <= cutoff}


def fetch_oi(symbol: str) -> list[tuple]:
    """拉 openInterestHist 1h（≤2 页），返回 [(ts, oi_usd), ...] 升序。

    openInterestHist 仅保留最近 30 天；窗口取 29 天防边界 400。
    若带时间参数仍被拒，降级为只取最新 500 条（≈20.8 天）。
    """
    rows: list[tuple] = []
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 29 * 86400 * 1000
    cursor = start_ms
    while cursor < end_ms and len(rows) < PAGE_LIMIT * 2:
        try:
            data = fapi_get(f"{FAPI_BASE}/futures/data/openInterestHist", {
                "symbol": symbol, "period": "1h", "startTime": cursor,
                "endTime": end_ms, "limit": PAGE_LIMIT,
            })
        except Exception:  # noqa: BLE001
            data = fapi_get(f"{FAPI_BASE}/futures/data/openInterestHist",
                            {"symbol": symbol, "period": "1h", "limit": PAGE_LIMIT})
        if not data:
            break
        for d in data:
            rows.append((datetime.fromtimestamp(d["timestamp"] / 1000.0, tz=timezone.utc),
                         float(d.get("sumOpenInterestValue") or 0)))
        cursor = int(data[-1]["timestamp"]) + 1
        if len(data) < PAGE_LIMIT:
            break
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="OI 1h 历史回填 → biz.oi_cvd_snapshot")
    parser.add_argument("--limit-symbols", type=int, default=0, help="只处理前 N 个符号")
    parser.add_argument("--dry-run", action="store_true", help="只拉取打印，不落库")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        symbols = get_universe(conn)
        covered = get_covered(conn)
        todo = [s for s in symbols if s not in covered]
        if args.limit_symbols:
            todo = todo[: args.limit_symbols]
        print(f"[oi-backfill] 宇宙 {len(symbols)} 符号，已覆盖 {len(covered)}，待回填 {len(todo)}")

        results: dict[str, list[tuple]] = {}
        errors = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(fetch_oi, s): s for s in todo}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    results[s] = fut.result()
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 10:
                        print(f"[warn] {s} 回填失败: {e}", file=sys.stderr)

        total = sum(len(v) for v in results.values())
        print(f"[oi-backfill] 完成 {len(results)} 符号，{total} 行，失败 {errors}")
        if args.dry_run:
            for s, rows in list(results.items())[:3]:
                print(f"  {s}: {len(rows)} 行，{rows[0][0].isoformat()} ~ {rows[-1][0].isoformat()}")
            return 0
        if not results:
            return 0
        all_rows = [(s, ts, oi) for s, rows in results.items() for ts, oi in rows]
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, all_rows)
        print(f"[db] upsert {len(all_rows)} 行 OI 历史")
    return 0


if __name__ == "__main__":
    sys.exit(main())
