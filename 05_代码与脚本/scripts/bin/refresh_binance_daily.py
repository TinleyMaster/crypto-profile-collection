"""刷新「币安日线」biz.asset_klines(interval='1d')，供每日变化榜消费。

背景（2026-10-12）：每日变化榜的行情口径从 CMC 切到币安。原设计是在生成器 SQL 里
把 biz.asset_klines 的 1h 聚合成日线，但实测有两个致命问题：
  1) open_time 上的索引对 `(open_time AT TIME ZONE 'UTC')::date` 不可用 → 全表 Seq Scan
     （单日查询 3.1s），4 年回填不可行；
  2) scan_daemon 的 1h 增量采集带 min_vol_usd=$5M 过滤且只取最近 10 根，导致最近若干天
     1h 覆盖从 527 个合约掉到 ~240 个，日线口径会跟着变薄。

改为：把日线**物化**进同一张表的 interval='1d' 分区（沿用既有主键，无需建表），
生成器只读 1d（行数 ~529×1460≈77 万，open_time 可直接走索引）。

两条数据通路：
    （默认）从币安 FAPI 拉权威 1d K 线          python bin/refresh_binance_daily.py
    --from-klines 离线：把库内 1h 聚合成 1d    python bin/refresh_binance_daily.py --from-klines
                  （无外网/被墙时可用；与 FAPI 的 1d 等价：收盘=23:00 UTC 的那根 1h，
                    成交额=当日 1h 之和）

用法：
    python bin/refresh_binance_daily.py --from-klines            # 离线全量种子（立即可用）
    python bin/refresh_binance_daily.py                          # 联机增量（补最近几天）
    python bin/refresh_binance_daily.py --start-date 2022-10-30  # 指定回填起点
    python bin/refresh_binance_daily.py --workers 4 --gap 0.3    # 并发（共享出口 IP 建议 1/0.8）
    python bin/refresh_binance_daily.py --direct                 # 绕过环境代理直连
    python bin/refresh_binance_daily.py --symbols BTCUSDT,ETHUSDT --dry-run
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
KLINES_PAGE = 1500          # fapi klines 单请求上限
TOLERANCE_H = 48            # 续跑容差：最新 1d bar 在 now-48h 内视为已覆盖
DEFAULT_START = "2022-10-30"

UPSERT_SQL = """
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
UPSERT_COLS = 10


def http_get_json(session: requests.Session, url: str, params: dict | None = None):
    """GET + 网络/限频指数退避（直连与代理下均可能抖动）。"""
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
        s["symbol"] for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    )


def fetch_daily_klines(session: requests.Session, symbol: str,
                       start_ms: int, end_ms: int, gap: float) -> list[tuple]:
    """分页拉取 1d K 线（4 年约 1460 根 < 1500，通常 1 页搞定）。"""
    rows: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        raw = http_get_json(session, f"{FAPI_BASE}/fapi/v1/klines", {
            "symbol": symbol, "interval": "1d",
            "startTime": cursor, "endTime": end_ms, "limit": KLINES_PAGE,
        })
        if not raw:
            break
        for k in raw:
            rows.append((
                symbol, "1d",
                datetime.fromtimestamp(int(k[0]) / 1000.0, tz=timezone.utc),
                float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                float(k[5]), float(k[7]), int(k[8]),
            ))
        nxt = int(raw[-1][0]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(gap)
    return rows


def covered_symbols(conn, symbols: list[str], start_ms: int) -> set[str]:
    """库内 1d 已覆盖：最早 ≤ 起点 且 最新 ≥ now-容差。"""
    tol = datetime.now(timezone.utc) - timedelta(hours=TOLERANCE_H)
    start_dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    out: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, MIN(open_time), MAX(open_time) FROM biz.asset_klines "
            "WHERE interval='1d' AND symbol = ANY(%s) GROUP BY symbol",
            (symbols,),
        )
        for sym, mn, mx in cur.fetchall():
            if mn is None or mx is None:
                continue
            # 合约上市晚于起点时，mn 只要求 ≤ 上市日（这里放宽：mn 只要求早于起点或已是最早可得）
            if mx >= tol and (mn <= start_dt or mn <= tol - timedelta(days=4000)):
                out.add(sym)
    return out


def upsert_batched(conn, rows: list[tuple], batch: int = 500) -> None:
    ph = ",".join(["%s"] * UPSERT_COLS)
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        values = ",".join(f"({ph}, NOW())" for _ in chunk)
        params = [v for row in chunk for v in row]
        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL.format(values=values), params)
    conn.commit()


# ── 离线：1h → 1d 聚合种子 ──

SEED_FROM_KLINES_SQL = """
INSERT INTO biz.asset_klines
    (symbol, interval, open_time, open_px, high_px, low_px, close_px,
     base_vol, quote_vol, trade_count, fetched_at)
SELECT
    symbol,
    '1d',
    (date_trunc('day', open_time AT TIME ZONE 'UTC')) AT TIME ZONE 'UTC' AS ot,
    (array_agg(open_px  ORDER BY open_time ASC ))[1] AS open_px,
    max(high_px) AS high_px,
    min(low_px)  AS low_px,
    (array_agg(close_px ORDER BY open_time DESC))[1] AS close_px,
    sum(base_vol)  AS base_vol,
    sum(quote_vol) AS quote_vol,
    sum(trade_count) AS trade_count,
    NOW()
FROM biz.asset_klines
WHERE interval = '1h'
  AND open_time >= %s::timestamptz
  AND open_time <  %s::timestamptz
GROUP BY symbol, ot
ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
    open_px=EXCLUDED.open_px, high_px=EXCLUDED.high_px, low_px=EXCLUDED.low_px,
    close_px=EXCLUDED.close_px, base_vol=EXCLUDED.base_vol,
    quote_vol=EXCLUDED.quote_vol, trade_count=EXCLUDED.trade_count,
    fetched_at=NOW()
"""


def seed_from_klines(conn, start: date, end: date, dry_run: bool) -> int:
    """把 [start, end] 的 1h 按 UTC 日聚合写入 1d（与 FAPI 1d 等价）。"""
    lo = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    hi = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    with conn.cursor() as cur:
        if dry_run:
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT symbol, (open_time AT TIME ZONE 'UTC')::date
                    FROM biz.asset_klines
                    WHERE interval='1h' AND open_time >= %s AND open_time < %s
                    GROUP BY 1, 2
                ) s
            """, (lo, hi))
            return int(cur.fetchone()[0])
        cur.execute(SEED_FROM_KLINES_SQL, (lo, hi))
        n = cur.rowcount
    conn.commit()
    return int(n)


def _worker(session, q: queue.Queue, out: queue.Queue, start_ms: int, end_ms: int,
            gap: float) -> None:
    while True:
        try:
            sym = q.get_nowait()
        except queue.Empty:
            return
        try:
            rows = fetch_daily_klines(session, sym, start_ms, end_ms, gap)
            out.put((sym, rows, None))
        except Exception as e:  # noqa: BLE001
            out.put((sym, [], f"{type(e).__name__}: {e}"))


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新币安日线（asset_klines interval='1d'）")
    ap.add_argument("--from-klines", action="store_true",
                    help="离线模式：把库内 1h 聚合成 1d（无外网可用）")
    ap.add_argument("--start-date", default=DEFAULT_START, help=f"回填起点（默认 {DEFAULT_START}）")
    ap.add_argument("--end-date", default=None, help="回填终点（默认今天 UTC）")
    ap.add_argument("--workers", type=int, default=4, help="并发数（共享出口 IP 建议 1）")
    ap.add_argument("--gap", type=float, default=0.3, help="单 worker 分页请求最小间隔秒")
    ap.add_argument("--symbols", default="", help="指定合约码（逗号分隔，覆盖全量）")
    ap.add_argument("--direct", action="store_true", help="绕过环境代理（trust_env=False）")
    ap.add_argument("--skip-pair", action="store_true", help="不刷新 asset_binance_pair 映射")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date) if args.end_date else datetime.now(timezone.utc).date()

    with get_connection(settings.database_url) as conn:
        if args.from_klines:
            print(f"[daily] 离线种子：1h → 1d，区间 {start} ~ {end}")
            n = seed_from_klines(conn, start, end, args.dry_run)
            print(f"[daily] {'(dry-run) 将写入' if args.dry_run else '已写入'} {n} 行 1d")
        else:
            session = requests.Session()
            if args.direct:
                session.trust_env = False
            symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                       if args.symbols else get_usdt_perpetuals(session))
            start_ms = int(datetime(start.year, start.month, start.day,
                                    tzinfo=timezone.utc).timestamp() * 1000)
            end_ms = int(time.time() * 1000)

            done = covered_symbols(conn, symbols, start_ms)
            todo = [s for s in symbols if s not in done]
            print(f"[daily] 合约 {len(symbols)} 个，已覆盖 {len(done)}，待拉 {len(todo)}")

            if args.dry_run:
                print(f"[daily] (dry-run) 将拉取 {len(todo)} 个合约的 1d K 线，不写库")
            elif todo:
                q: queue.Queue = queue.Queue()
                for s in todo:
                    q.put(s)
                out: queue.Queue = queue.Queue()
                threads = []
                for _ in range(max(1, args.workers)):
                    sess = requests.Session()
                    if args.direct:
                        sess.trust_env = False
                    t = threading.Thread(target=_worker,
                                         args=(sess, q, out, start_ms, end_ms, args.gap))
                    t.start()
                    threads.append(t)

                total, errs = 0, 0
                for _ in range(len(todo)):
                    sym, rows, err = out.get()
                    if err:
                        errs += 1
                        print(f"[daily]   {sym} 失败：{err}")
                    elif rows:
                        upsert_batched(conn, rows)
                        total += len(rows)
                for t in threads:
                    t.join()
                print(f"[daily] 完成：写入 {total} 行 1d，失败 {errs} 个合约")
            else:
                print("[daily] 无需拉取")

        if not args.skip_pair and not args.dry_run:
            print("[daily] 刷新 asset_binance_pair 映射 …")
            subprocess.run([sys.executable, str(SCRIPT_DIR / "refresh_binance_pair.py")],
                           check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())