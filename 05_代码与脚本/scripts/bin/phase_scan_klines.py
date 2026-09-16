#!/usr/bin/env python3
"""盘面异动扫描 P0 · K 线采集：Binance USDT 永续 5m/15m/1h → biz.asset_klines。

L1 粗筛与回测的数据源。增量模式每次拉每币每周期最近 10 根并 upsert；
--backfill 模式按 startTime 分页回填历史（供 P1 回测）。

用法：
    python phase_scan_klines.py                          # 增量：全量 USDT 永续，5m/15m/1h
    python phase_scan_klines.py --min-vol-usd 5000000    # 仅 24h 成交额 ≥500 万美元
    python phase_scan_klines.py --top 200                # 仅市值 top200（与 core.asset 对齐）
    python phase_scan_klines.py --intervals 5m,1h        # 指定周期
    python phase_scan_klines.py --limit-symbols 5 --dry-run   # 冒烟：只拉 5 个币、不落库
    python phase_scan_klines.py --backfill-days 90 --intervals 1h   # 回填 90 天 1h
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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
from crypto_research.db.upsert import execute_many  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
TIMEOUT = 20
MAX_RETRIES = 3                # 网络抖动重试
MIN_REQUEST_GAP = 0.3         # 全局请求最小间隔（秒），≈3.3 req/s，保守防 Binance 限频封禁
DEFAULT_INTERVALS = ("5m", "15m", "1h")
INCREMENTAL_LIMIT = 10          # 增量模式：每币每周期拉最近 N 根
BACKFILL_PAGE_LIMIT = 1500      # 回填模式：单请求最大 K 线数
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}

_SESSION = requests.Session()   # 连接复用，降低被服务器断连概率
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0

UPSERT_SQL = """
    INSERT INTO biz.asset_klines
        (symbol, interval, open_time, open_px, high_px, low_px, close_px,
         base_vol, quote_vol, trade_count, fetched_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
        open_px=EXCLUDED.open_px, high_px=EXCLUDED.high_px, low_px=EXCLUDED.low_px,
        close_px=EXCLUDED.close_px, base_vol=EXCLUDED.base_vol,
        quote_vol=EXCLUDED.quote_vol, trade_count=EXCLUDED.trade_count,
        fetched_at=NOW()
"""


def _get(url: str, params: dict) -> list:
    """带全局限频 + 429 指数退避 + 网络重试的 GET。"""
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
                wait = min(5 * (2 ** attempt), 60)
                print(f"[throttle] 429 限频，等待 {wait}s 后重试", file=sys.stderr)
                time.sleep(wait)
                last_err = RuntimeError(f"429 rate limited (attempt {attempt})")
                continue
            if r.status_code == 418:
                # 418 = IP 被封禁（超限惩罚），需等更久
                wait = 60 * (attempt + 1)
                print(f"[throttle] 418 IP 封禁，等待 {wait}s 后重试", file=sys.stderr)
                time.sleep(wait)
                last_err = RuntimeError(f"418 ip banned (attempt {attempt})")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < MAX_RETRIES + 1:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


def get_covered(conn, interval: str, end_ms: int) -> set[str]:
    """回填续跑：返回已覆盖回填窗口（end 前 2 根内）的符号集合。"""
    tol = datetime.fromtimestamp((end_ms - 2 * INTERVAL_SECONDS[interval] * 1000) / 1000.0,
                                 tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, MAX(open_time) AS last_ot FROM biz.asset_klines "
            "WHERE interval = %s GROUP BY symbol",
            (interval,),
        )
        return {sym for sym, last_ot in cur.fetchall() if last_ot >= tol}


def get_usdt_perpetuals() -> list[str]:
    """返回 Binance 全部 TRADING 状态的 USDT 永续合约符号。"""
    data = _get(f"{FAPI_BASE}/fapi/v1/exchangeInfo", {})
    syms = [
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    ]
    return sorted(syms)


def get_24h_quote_volume() -> dict[str, float]:
    """返回 {symbol: 24h 成交额(USDT)}。"""
    data = _get(f"{FAPI_BASE}/fapi/v1/ticker/24hr", {})
    return {row["symbol"]: float(row.get("quoteVolume") or 0) for row in data}


def get_core_asset_symbols(conn, top: int) -> set[str]:
    """从 core.asset 取市值 top N 的 canonical_symbol（与现有投研库对齐）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_symbol FROM core.asset
            WHERE canonical_symbol IS NOT NULL AND market_cap_rank IS NOT NULL
            ORDER BY market_cap_rank ASC LIMIT %s
            """,
            (top,),
        )
        return {r[0].upper() for r in cur.fetchall()}


def parse_klines_rows(symbol: str, interval: str, raw: list) -> list[tuple]:
    """Binance klines → 入库行。"""
    rows = []
    for k in raw:
        open_ms = int(k[0])
        rows.append((
            symbol, interval,
            datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc),
            k[1], k[2], k[3], k[4],          # open/high/low/close
            k[5], k[7], k[8],                # base_vol / quote_vol / trade_count
        ))
    return rows


def fetch_incremental(symbol: str, interval: str) -> list[tuple]:
    raw = _get(f"{FAPI_BASE}/fapi/v1/klines",
               {"symbol": symbol, "interval": interval, "limit": INCREMENTAL_LIMIT})
    return parse_klines_rows(symbol, interval, raw)


def fetch_backfill(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[tuple]:
    rows: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        raw = _get(f"{FAPI_BASE}/fapi/v1/klines", {
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": BACKFILL_PAGE_LIMIT,
        })
        if not raw:
            break
        rows.extend(parse_klines_rows(symbol, interval, raw))
        cursor = int(raw[-1][0]) + 1
        time.sleep(0.05)  # 回填节流
    return rows


def build_symbols(args, conn) -> list[str]:
    """确定本次扫描的合约列表：Binance USDT 永续 ∩ 可选过滤。"""
    syms = get_usdt_perpetuals()
    if args.top:
        core = get_core_asset_symbols(conn, args.top)
        syms = [s for s in syms if s in core]
        print(f"[symbols] 市值 top{args.top} 过滤后 {len(syms)} 个")
    if args.min_vol_usd:
        vol_map = get_24h_quote_volume()
        syms = [s for s in syms if vol_map.get(s, 0) >= args.min_vol_usd]
        print(f"[symbols] 24h 成交额 ≥{args.min_vol_usd:,.0f} 过滤后 {len(syms)} 个")
    if args.limit_symbols:
        syms = syms[: args.limit_symbols]
    return syms


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance USDT 永续 K 线采集 → biz.asset_klines")
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS),
                        help="周期列表，逗号分隔（默认 5m,15m,1h）")
    parser.add_argument("--top", type=int, default=0,
                        help="仅拉市值 top N（对齐 core.asset，0=不限制）")
    parser.add_argument("--min-vol-usd", type=float, default=0.0,
                        help="仅拉 24h 成交额 ≥ 该值（USDT）的合约")
    parser.add_argument("--limit-symbols", type=int, default=0,
                        help="只处理前 N 个符号（冒烟测试用）")
    parser.add_argument("--backfill-days", type=int, default=0,
                        help="回填最近 N 天历史 K 线（>0 时进入回填模式）")
    parser.add_argument("--dry-run", action="store_true", help="只拉取打印，不写库")
    parser.add_argument("--workers", type=int, default=8, help="并发数（默认 8）")
    args = parser.parse_args()

    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        symbols = build_symbols(args, conn)
        print(f"[symbols] 共 {len(symbols)} 个合约，周期 {intervals}，"
              f"模式={'回填' + str(args.backfill_days) + '天' if args.backfill_days else '增量'}")

        tasks = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            if args.backfill_days:
                end_ms = int(time.time() * 1000)
                start_ms = end_ms - args.backfill_days * 86400 * 1000
                for iv in intervals:
                    covered = get_covered(conn, iv, end_ms)
                    n_skip = sum(1 for s in symbols if s in covered)
                    for sym in symbols:
                        if sym in covered:
                            continue
                        tasks.append(pool.submit(fetch_backfill, sym, iv, start_ms, end_ms))
                    print(f"[symbols] {iv} 续跑跳过已覆盖 {n_skip} 个，待拉 {len(symbols) - n_skip} 个")
            else:
                for sym in symbols:
                    for iv in intervals:
                        tasks.append(pool.submit(fetch_incremental, sym, iv))

            all_rows: list[tuple] = []
            errors = 0
            for fut in as_completed(tasks):
                try:
                    rows = fut.result()
                    all_rows.extend(rows)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 10:
                        print(f"[warn] 拉取失败: {e}", file=sys.stderr)

        print(f"[fetch] 完成，共 {len(all_rows)} 根 K 线，失败 {errors} 个任务")
        if args.dry_run:
            for r in all_rows[:5]:
                print("  样例:", r[0], r[1], r[2].isoformat(), r[5])
            return 0
        if errors and not all_rows:
            return 1
        if not all_rows:
            return 0

        execute_many(conn, UPSERT_SQL, all_rows)
        print(f"[db] upsert {len(all_rows)} 行完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
