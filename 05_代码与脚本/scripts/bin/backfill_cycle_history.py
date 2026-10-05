#!/usr/bin/env python3
"""涨幅榜周期回测 · 历史回填：Binance USDT 永续 1h K 线 + 资金费率（2023-01-01 起）。

背景（2026-10-03）：现有 1h K 线仅 2026-06-18 起（~3.5 个月），不足一个完整 4 年周期。
本脚本把 1h K 线 + 资金费率回填到一个完整牛市周期起点（默认 2023-01-01，
覆盖 2023 熊底 → 2024 减半 → 2025 牛市顶 → 2026 调整），供 backtest_binance_gainers.py
与 backtest_hourly_reversal.py 做跨周期稳健性验证。

数据源：Binance FAPI 公开接口（无需 key）。

实现（2026-10-04 第四版，直连 + 多 worker 定稿）：
  - v1 并发（共享 binance_http 全局锁）：全局锁串行化，纯并发无收益
  - v2 自建 fast 锁并发：所有 worker 阻塞在锁等待（sample 证实），无完成落库
  - v3 串行：稳定但被 SOCKS 代理（HTTPS_PROXY=socks5://127.0.0.1:7898）抖动拖慢
    （ReadTimeout/SSLError 重试频繁，~0.6 符号/min，全量 ~11h）
  - v4 定稿：**直连 + 每 worker 独立 requests.Session**（trust_env=False 绕过代理，
    实测直连 0.87s/页稳定）+ 多 worker 各自限频（0.12s 页间隔 → 全局 ~3.3 req/s，
    在 Binance fapi 2400 weight/min 限额内）→ 理论 ~50 分钟完成全部。
  - 每符号完成后主线程立即 upsert + commit（可续跑，已入库符号自动跳过）。

体量（529 合约 × ~3.8 年）：
  - 1h K 线 ≈ 1760 万行；资金费率 ≈ 190 万条；存储 ≈ 1.7GB。

用法：
    python backfill_cycle_history.py                          # 直连 + 4 worker
    python backfill_cycle_history.py --proxy --workers 1      # 走代理串行（回退）
    python backfill_cycle_history.py --no-funding             # 只回填 K 线
    python backfill_cycle_history.py --limit-symbols 3 --dry-run   # 冒烟
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg  # noqa: E402
import requests  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
DEFAULT_START = "2023-01-01"
KLINES_PAGE = 1500          # klines 单请求上限
FUNDING_PAGE = 1000         # fundingRate 单请求上限
TOLERANCE_H = 48            # 续跑容差：已覆盖到 now-48h 内视为已覆盖
PAGE_GAP_S = 0.25           # 每 worker 分页请求最小间隔（3 worker → 全局 ~2.4 req/s ≈
                            # 1440 weight/min，klines limit=1500 权重 10/请求，
                            # 4 worker×0.12s 实测打满 2400/min 触发 429 → 调 3 worker 0.25s）


def make_session(direct: bool) -> requests.Session:
    s = requests.Session()
    if direct:
        s.trust_env = False   # 绕过环境代理（HTTPS_PROXY=socks5://127.0.0.1:7898），
                              # 直连实测 0.87s/页稳定；走代理抖动频繁 ReadTimeout/SSLError
    return s


def http_get_json(session: requests.Session, url: str, params: dict | None = None):
    """GET + 网络/限频指数重试（直连后仍偶发抖动，须重试）。"""
    attempt = 0
    while True:
        try:
            r = session.get(url, params=params or {}, timeout=30)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.SSLError) as e:
            wait = min(5 * (2 ** min(attempt, 4)), 60)
            print(f"[http] 网络异常重试: {type(e).__name__}，退避 {wait:.0f}s", flush=True)
            attempt += 1
            time.sleep(wait)
            continue
        if r.status_code in (429, 418):
            wait = min(30 * (2 ** min(attempt, 4)), 300)
            print(f"[http] {r.status_code} 限频，退避 {wait:.0f}s", flush=True)
            attempt += 1
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


def get_usdt_perpetuals(session: requests.Session) -> list[str]:
    data = http_get_json(session, f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    return sorted(
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    )


def get_onboard_dates(session: requests.Session) -> dict[str, int]:
    data = http_get_json(session, f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    return {s["symbol"]: int(s["onboardDate"]) for s in data.get("symbols", [])
            if s.get("onboardDate")}


def fetch_klines(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """分页拉取 1h K 线。"""
    rows: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        raw = http_get_json(session, f"{FAPI_BASE}/fapi/v1/klines", {
            "symbol": symbol, "interval": "1h",
            "startTime": cursor, "endTime": end_ms, "limit": KLINES_PAGE,
        })
        if not raw:
            break
        for k in raw:
            rows.append((
                symbol, "1h",
                datetime.fromtimestamp(int(k[0]) / 1000.0, tz=timezone.utc),
                k[1], k[2], k[3], k[4], k[5], k[7], k[8],
            ))
        cursor = int(raw[-1][0]) + 1
        time.sleep(PAGE_GAP_S)
    return rows


def fetch_funding(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """分页拉取资金费率（8h 结算，历史可任意回溯）。"""
    rows: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        raw = http_get_json(session, f"{FAPI_BASE}/fapi/v1/fundingRate", {
            "symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": FUNDING_PAGE,
        })
        if not raw:
            break
        for d in raw:
            rows.append((
                symbol,
                datetime.fromtimestamp(int(d["fundingTime"]) / 1000.0, tz=timezone.utc),
                float(d["fundingRate"]),
            ))
        cursor = int(raw[-1]["fundingTime"]) + 1
        time.sleep(PAGE_GAP_S)
    return rows


def klines_covered(conn, symbols: list[str], start_ms: int, session: requests.Session) -> set[str]:
    """已覆盖 = 库内最早已 ≤ 有效起点（max(上市日, 窗口起点)）且最新 ≥ now-容差。"""
    onboard = get_onboard_dates(session)
    tol = datetime.now(timezone.utc) - timedelta(hours=TOLERANCE_H)
    out: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, MIN(open_time), MAX(open_time) FROM biz.asset_klines "
            "WHERE interval='1h' AND symbol = ANY(%s) GROUP BY symbol",
            (symbols,),
        )
        for sym, mn, mx in cur.fetchall():
            if mn is None or mx is None or mx < tol:
                continue
            limit = max(onboard.get(sym, 0), start_ms)
            if mn <= datetime.fromtimestamp(limit / 1000.0, tz=timezone.utc):
                out.add(sym)
    return out


def funding_covered(conn, symbols: list[str]) -> set[str]:
    tol = datetime.now(timezone.utc) - timedelta(hours=TOLERANCE_H)
    out: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, MAX(funding_time) FROM biz.funding_rate_hist "
            "WHERE symbol = ANY(%s) GROUP BY symbol",
            (symbols,),
        )
        out = {sym for sym, mx in cur.fetchall() if mx is not None and mx >= tol}
    return out


KLINES_UPSERT = """
    INSERT INTO biz.asset_klines
        (symbol, interval, open_time, open_px, high_px, low_px, close_px,
         base_vol, quote_vol, trade_count, fetched_at)
    VALUES {values}
    ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
        open_px=EXCLUDED.open_px, high_px=EXCLUDED.high_px, low_px=EXCLUDED.low_px,
        close_px=EXCLUDED.close_px, base_vol=EXCLUDED.base_vol,
        quote_vol=EXCLUDED.quote_vol, trade_count=EXCLUDED.trade_count,
        fetched_at=NOW()
"""
KLINES_COLS = 10
KLINES_TAIL = "NOW()"

FUNDING_UPSERT = """
    INSERT INTO biz.funding_rate_hist (symbol, funding_time, rate, source_code, fetched_at)
    VALUES {values}
    ON CONFLICT (symbol, funding_time) DO UPDATE SET
        rate=EXCLUDED.rate, fetched_at=NOW()
"""
FUNDING_COLS = 3
FUNDING_TAIL = "'binance', NOW()"


def upsert_batched(conn, sql_tpl: str, n_cols: int, tail: str, rows: list[tuple],
                   batch: int = 500) -> None:
    """批量多值 INSERT（vs psycopg executemany 逐条 round-trip，快 ~10 倍）。"""
    ph = ",".join(["%s"] * n_cols)
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        values = ",".join(f"({ph}, {tail})" for _ in chunk)
        sql = sql_tpl.format(values=values)
        params = [v for row in chunk for v in row]
        with conn.cursor() as cur:
            cur.execute(sql, params)
    conn.commit()


def _worker(session: requests.Session, q: queue.Queue, out: queue.Queue,
            fetch_fn, start_ms: int, end_ms: int) -> None:
    while True:
        try:
            sym = q.get_nowait()
        except queue.Empty:
            return
        try:
            rows = fetch_fn(session, sym, start_ms, end_ms)
            out.put((sym, rows, None))
        except Exception as e:  # noqa: BLE001
            out.put((sym, None, f"{type(e).__name__}: {e}"))


def run_workers(conn, fetch_fn, todo: list[str], start_ms: int, end_ms: int,
                label: str, sql_tpl: str, n_cols: int, tail: str, args) -> int:
    """直连 + 多 worker：每 worker 独立 session 串行处理队列符号（无共享锁），
    主线程按完成顺序批量 upsert + commit。返回成功符号数。"""
    if args.workers <= 1:
        # 单 worker = 串行（与多 worker 同一路径，仅 1 线程）
        pass
    q: queue.Queue = queue.Queue()
    for s in todo:
        q.put(s)
    out: queue.Queue = queue.Queue()
    sessions = [make_session(not args.proxy) for _ in range(args.workers)]
    threads = [
        threading.Thread(target=_worker, args=(s, q, out, fetch_fn, start_ms, end_ms),
                         daemon=True)
        for s in sessions
    ]
    for t in threads:
        t.start()

    ok = 0
    pending = len(todo)
    errors = 0
    while pending > 0:
        sym, rows, err = out.get()
        pending -= 1
        if err:
            errors += 1
            print(f"[warn] {sym} 失败: {err}", flush=True)
            continue
        if rows:
            if not args.dry_run:
                upsert_batched(conn, sql_tpl, n_cols, tail, rows)
            ok += 1
        if pending % 10 == 0 or pending < 5:
            print(f"[{label}] 剩余 {pending} 符号（累计成功 {ok}，失败 {errors}）", flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="涨幅榜周期回测历史回填（1h K线 + 资金费率）")
    parser.add_argument("--start-date", default=DEFAULT_START, help="回填起点日期（默认 2023-01-01）")
    parser.add_argument("--no-funding", action="store_true", help="只回填 K 线，跳过资金费率")
    parser.add_argument("--limit-symbols", type=int, default=0, help="只处理前 N 个符号（冒烟）")
    parser.add_argument("--dry-run", action="store_true", help="只抓取打印，不写库")
    parser.add_argument("--workers", type=int, default=3, help="并发 worker 数（默认 3，>3 易触发 429）")
    parser.add_argument("--proxy", action="store_true", help="走环境代理（默认直连，绕过 socks 抖动）")
    args = parser.parse_args()

    start = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(time.time() * 1000)

    settings = get_settings(require_database=True)
    boot = make_session(not args.proxy)
    symbols = get_usdt_perpetuals(boot)
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]
    print(f"[symbols] {len(symbols)} 个 USDT 永续，窗口 {args.start_date} ~ now，"
          f"funding={'开' if not args.no_funding else '关'}，"
          f"{args.workers} worker，{'直连' if not args.proxy else '代理'}")

    # 重连外壳：远程连接偶发被服务端关闭，断开后重跑整轮（已完成符号自动跳过）
    attempt = 0
    while True:
        attempt += 1
        try:
            with get_connection(settings.database_url) as conn:
                # ---- 1h K 线 ----
                covered = klines_covered(conn, symbols, start_ms, boot)
                todo = [s for s in symbols if s not in covered]
                print(f"[klines] 已覆盖跳过 {len(covered)}，待拉 {len(todo)}")
                if todo:
                    t0 = time.time()
                    ok = run_workers(conn, fetch_klines, todo, start_ms, end_ms,
                                     "klines", KLINES_UPSERT, KLINES_COLS, KLINES_TAIL, args)
                    print(f"[klines] 完成 {ok}/{len(todo)} 符号，耗时 {(time.time()-t0)/60:.1f} 分钟")

                # ---- 资金费率 ----
                if not args.no_funding:
                    fcov = funding_covered(conn, symbols)
                    ftodo = [s for s in symbols if s not in fcov]
                    print(f"[funding] 已覆盖跳过 {len(fcov)}，待拉 {len(ftodo)}")
                    if ftodo:
                        t0 = time.time()
                        ok = run_workers(conn, fetch_funding, ftodo, start_ms, end_ms,
                                         "funding", FUNDING_UPSERT, FUNDING_COLS, FUNDING_TAIL, args)
                        print(f"[funding] 完成 {ok}/{len(ftodo)} 符号，耗时 {(time.time()-t0)/60:.1f} 分钟")
            return 0
        except psycopg.OperationalError as e:
            print(f"[conn] 第 {attempt} 次连接异常: {e}，5s 后整轮重试（已入库符号自动跳过）",
                  flush=True)
            time.sleep(5)
            if attempt > 50:
                raise


if __name__ == "__main__":
    sys.exit(main())
