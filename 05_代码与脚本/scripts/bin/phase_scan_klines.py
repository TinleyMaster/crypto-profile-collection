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
    python phase_scan_klines.py --backfill-days 3 --force           # 采集停摆后补缺口（忽略续跑跳过）
    python phase_scan_klines.py --backfill-days 1380 --fill-earlier --intervals 1h
                                                        # 扩展历史：只补比现有最早一根更早的窗口
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
from crypto_research.db.upsert import execute_many  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
DEFAULT_INTERVALS = ("5m", "15m", "1h")
INCREMENTAL_LIMIT = 10          # 增量模式：每币每周期拉最近 N 根
BACKFILL_PAGE_LIMIT = 1500      # 回填模式：单请求最大 K 线数
FLUSH_ROWS = 50_000             # 回填分批落库阈值（约 0.5KB/行，避免千万行驻留内存）
TASK_BATCH = 16                 # 每批提交的任务数（批内跑完释放 future，约 350MB 上限）
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}

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


def get_earliest(conn, interval: str) -> dict[str, datetime]:
    """返回每个符号已入库的最早 open_time（`--fill-earlier` 用于定位待补缺口）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, MIN(open_time) FROM biz.asset_klines "
            "WHERE interval = %s GROUP BY symbol",
            (interval,),
        )
        return dict(cur.fetchall())


def get_usdt_perpetuals() -> list[str]:
    """返回 Binance 全部 TRADING 状态的 USDT 永续合约符号。"""
    data = fapi_get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
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
    data = fapi_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
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
    raw = fapi_get(f"{FAPI_BASE}/fapi/v1/klines",
                   {"symbol": symbol, "interval": interval, "limit": INCREMENTAL_LIMIT})
    return parse_klines_rows(symbol, interval, raw)


def fetch_backfill(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[tuple]:
    rows: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        raw = fapi_get(f"{FAPI_BASE}/fapi/v1/klines", {
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
    parser.add_argument("--force", action="store_true",
                        help="回填模式忽略续跑跳过（用于采集停摆后的缺口补填：续跑只看最新 K 线"
                             "是否新鲜，缺口在中间时会被误判为已覆盖）")
    parser.add_argument("--fill-earlier", action="store_true",
                        help="每币只回填「比库里现有最早一根更早」的窗口，已覆盖区间不重抓不重写"
                             "（扩展历史用；--force 是补中间缺口，两者用途不同）")
    parser.add_argument("--dry-run", action="store_true", help="只拉取打印，不写库")
    parser.add_argument("--workers", type=int, default=8, help="并发数（默认 8）")
    args = parser.parse_args()

    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        symbols = build_symbols(args, conn)
        print(f"[symbols] 共 {len(symbols)} 个合约，周期 {intervals}，"
              f"模式={'回填' + str(args.backfill_days) + '天' if args.backfill_days else '增量'}")

        # 任务规格：**不预先 submit**。一次性 submit 全部任务会让 tasks 列表持有所有
        # future 的强引用，而已完成的 future 会一直保留自己的 result（每币 1.5~3.3 万行
        # ≈ 10~22MB），回填 1380 天 × 500+ 合约时会有上千万行常驻内存直至 OOM。
        specs: list[tuple] = []
        if args.backfill_days:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - args.backfill_days * 86400 * 1000
            for iv in intervals:
                # --fill-earlier：目标是补「比库里现有最早更早」的窗口，与最新 K 线
                # 是否新鲜无关——因此必须忽略 covered 续跑跳过，否则每天增量采集的
                # 币（covered=True）会被挡在回填之外，历史缺口永远补不上。
                covered = set() if (args.force or args.fill_earlier) else get_covered(conn, iv, end_ms)
                n_skip = sum(1 for s in symbols if s in covered)
                # --fill-earlier：每币只回填「比库里现有最早一根更早」的窗口，
                # 已覆盖区间不重抓、不重写（重复 upsert 会让表体积虚胀）。
                earliest = get_earliest(conn, iv) if args.fill_earlier else {}
                n_uptodate = 0
                for sym in symbols:
                    if sym in covered:
                        continue
                    sym_end_ms = end_ms
                    if args.fill_earlier:
                        first_ot = earliest.get(sym)
                        sym_end_ms = int(first_ot.timestamp() * 1000) if first_ot else end_ms
                        if sym_end_ms <= start_ms:
                            n_uptodate += 1
                            continue
                    specs.append((fetch_backfill, sym, iv, start_ms, sym_end_ms))
                print(f"[symbols] {iv} 续跑跳过已覆盖 {n_skip} 个，待拉 "
                      f"{len(symbols) - n_skip - n_uptodate} 个"
                      + (f"，已早于窗口起点无需回填 {n_uptodate} 个" if args.fill_earlier else ""))
        else:
            for sym in symbols:
                for iv in intervals:
                    specs.append((fetch_incremental, sym, iv))

        all_rows: list[tuple] = []
        errors = 0
        fetched = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            # 分批提交：每批 TASK_BATCH 个，批内跑完即释放 future 及其结果。
            for i in range(0, len(specs), TASK_BATCH):
                futures = [pool.submit(fn, *rest) for fn, *rest in specs[i:i + TASK_BATCH]]
                for fut in as_completed(futures):
                    try:
                        rows = fut.result()
                    except Exception as e:  # noqa: BLE001
                        errors += 1
                        if errors <= 10:
                            print(f"[warn] 拉取失败: {e}", file=sys.stderr)
                        continue
                    fetched += len(rows)
                    all_rows.extend(rows)
                    # 分批落库：缓冲上限 FLUSH_ROWS，避免千万行驻留内存（约 0.5KB/行）。
                    if not args.dry_run and len(all_rows) >= FLUSH_ROWS:
                        execute_many(conn, UPSERT_SQL, all_rows)
                        # 每批独立提交：get_connection 只在 with 退出时 commit 一次，
                        # 不显式提交则全部批次同处一个事务 —— 中途崩溃会整体回滚，
                        # 且长时间持锁会拖住并发写入者（实测 lock_timeout 30s 触发）。
                        conn.commit()
                        print(f"[db] upsert {len(all_rows)} 行（累计抓取 {fetched} 根）")
                        all_rows.clear()
                futures = []  # 释放本批 future，切断其对 result 的引用

        print(f"[fetch] 完成，共 {fetched} 根 K 线，失败 {errors} 个任务")
        if args.dry_run:
            for r in all_rows[:5]:
                print("  样例:", r[0], r[1], r[2].isoformat(), r[5])
            return 0
        if errors and not fetched:
            return 1
        if not fetched:
            return 0

        if all_rows:
            execute_many(conn, UPSERT_SQL, all_rows)
            print(f"[db] upsert {len(all_rows)} 行（末批）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
