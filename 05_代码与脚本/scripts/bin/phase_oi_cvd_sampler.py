#!/usr/bin/env python3
"""盘面异动扫描 P0 · OI/CVD 采样：Binance USDT 永续 → biz.oi_cvd_snapshot。

每 5 分钟采样一轮：OI 总价值（openInterest × markPrice）+ 增量 CVD
（基于 aggTrades 游标，正=主动买盘强）+ 窗口总成交额，落 5m 桶；
随后回填 cvd_1h（近 12 桶累计）。

局限（P0 已知）：单个采样窗口最多拉 2 页 aggTrades（约 2000 笔），
超高频合约（BTC/ETH 等）存在低估，中低频标的不受影响；后续可升级为
WebSocket 全量流式 CVD。

用法：
    python phase_oi_cvd_sampler.py                          # 单轮采样（全部 USDT 永续）
    python phase_oi_cvd_sampler.py --limit-symbols 5        # 冒烟：只采样 5 个币
    python phase_oi_cvd_sampler.py --watch --interval 300   # 常驻每 5 分钟一轮
    python phase_oi_cvd_sampler.py --dry-run                # 只拉取打印，不落库
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
AGGTRADES_MAX_PAGES = 2     # 每个符号最多拉 2 页 aggTrades（约 2000 笔）
BUCKET_SECONDS = 300        # 5 分钟桶


def get_usdt_perpetuals() -> list[str]:
    data = fapi_get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    return sorted(
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    )


def get_24h_quote_volume() -> dict[str, float]:
    """返回 {symbol: 24h 成交额(USDT)}，用于流动性过滤。"""
    data = fapi_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
    return {row["symbol"]: float(row.get("quoteVolume") or 0) for row in data}


def load_cursors(conn) -> dict[str, int]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT symbol, last_trade_id FROM biz.scan_sampler_state")
        return {r["symbol"]: r["last_trade_id"] for r in cur.fetchall()}


def sample_symbol(symbol: str, cursor: int) -> dict:
    """采样单个合约：返回 {symbol, oi_usd, cvd_5m, vol_5m, last_trade_id}。

    游标缺失（首次运行）时只取最近 aggTrades 种子游标，CVD/vol 记 None，
    避免把"启动前窗口"误算进第一个桶。
    """
    result: dict = {"symbol": symbol, "oi_usd": None, "cvd_5m": None,
                    "vol_5m": None, "last_trade_id": cursor}

    # 1) OI 价值 = openInterest qty × markPrice
    try:
        oi_data = fapi_get(f"{FAPI_BASE}/fapi/v1/openInterest", {"symbol": symbol})
        mark = fapi_get(f"{FAPI_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
        qty = float(oi_data.get("openInterest") or 0)
        price = float(mark.get("markPrice") or 0)
        result["oi_usd"] = qty * price
    except Exception:
        pass  # OI 失败不阻塞 CVD

    # 2) 增量 CVD：从游标之后的 aggTrades 求和
    try:
        signed = 0.0
        total = 0.0
        last_id = cursor
        from_id = cursor + 1 if cursor > 0 else None
        params: dict = {"symbol": symbol, "limit": 1000}
        if from_id:
            params["fromId"] = from_id
        else:
            # 种子模式：只定位游标，不累计
            seed = fapi_get(f"{FAPI_BASE}/fapi/v1/aggTrades",
                            {"symbol": symbol, "limit": 1000})
            if seed:
                result["last_trade_id"] = max(int(t["a"]) for t in seed)
            return result

        for _ in range(AGGTRADES_MAX_PAGES):
            page = fapi_get(f"{FAPI_BASE}/fapi/v1/aggTrades", params)
            if not page:
                break
            for t in page:
                price_t = float(t["p"])
                qty_t = float(t["q"])
                signed += (-price_t * qty_t) if t.get("m") else (price_t * qty_t)
                total += price_t * qty_t
            last_id = max(last_id, int(page[-1]["a"]))
            if len(page) < 1000:
                break
            params["fromId"] = last_id + 1
        result["cvd_5m"] = signed
        result["vol_5m"] = total
        result["last_trade_id"] = last_id
    except Exception:
        pass
    return result


def bucket_ts(now: datetime) -> datetime:
    """对齐到 5 分钟桶边界。"""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % BUCKET_SECONDS, tz=timezone.utc)


def run_once(settings, args) -> tuple[int, int]:
    symbols = get_usdt_perpetuals()
    if args.min_vol_usd:
        vol_map = get_24h_quote_volume()
        symbols = [s for s in symbols if vol_map.get(s, 0) >= args.min_vol_usd]
        print(f"[sampler] 24h 成交额 ≥{args.min_vol_usd:,.0f} 过滤后 {len(symbols)} 个")
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    with get_connection(settings.database_url) as conn:
        cursors = load_cursors(conn)
        ts = bucket_ts(datetime.now(timezone.utc))

        results: list[dict] = []
        errors = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(sample_symbol, s, cursors.get(s, 0)): s for s in symbols}
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    r = fut.result()
                    results.append(r)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 10:
                        print(f"[warn] {s} 采样失败: {e}", file=sys.stderr)

        if args.dry_run:
            for r in results[:5]:
                print("  样例:", r["symbol"], "oi_usd=", r["oi_usd"],
                      "cvd_5m=", r["cvd_5m"], "vol_5m=", r["vol_5m"])
            return len(results), errors

        # 落库：桶 + 游标
        rows = []
        for r in results:
            rows.append((r["symbol"], ts, "binance", r["oi_usd"],
                         r["cvd_5m"], None, r["vol_5m"]))
        state_rows = [(r["symbol"], r["last_trade_id"],
                       datetime.now(timezone.utc)) for r in results]

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO biz.oi_cvd_snapshot
                    (symbol, ts, exchange, oi_usd, cvd_5m_usd, cvd_1h_usd, vol_5m_usd)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, exchange, ts) DO UPDATE SET
                    oi_usd=EXCLUDED.oi_usd, cvd_5m_usd=EXCLUDED.cvd_5m_usd,
                    cvd_1h_usd=EXCLUDED.cvd_1h_usd, vol_5m_usd=EXCLUDED.vol_5m_usd
                """,
                rows,
            )
            cur.executemany(
                """
                INSERT INTO biz.scan_sampler_state (symbol, last_trade_id, updated_at)
                VALUES (%s,%s,%s)
                ON CONFLICT (symbol) DO UPDATE SET
                    last_trade_id=EXCLUDED.last_trade_id, updated_at=EXCLUDED.updated_at
                """,
                state_rows,
            )
            # 回填近 1h 的 cvd_1h（近 12 桶累计）
            cur.execute(
                """
                UPDATE biz.oi_cvd_snapshot s SET cvd_1h_usd = w.c1h
                FROM (
                    SELECT symbol, ts,
                           SUM(cvd_5m_usd) OVER (
                               PARTITION BY symbol ORDER BY ts
                               ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS c1h
                    FROM biz.oi_cvd_snapshot
                    WHERE ts >= NOW() - INTERVAL '2 hours'
                ) w
                WHERE s.symbol = w.symbol AND s.ts = w.ts
                  AND s.ts >= NOW() - INTERVAL '1 hour'
                """
            )
        print(f"[sampler] 桶 {ts.isoformat()} 采样 {len(rows)} 个合约，失败 {errors}")
        return len(rows), errors


def main() -> int:
    parser = argparse.ArgumentParser(description="OI/CVD 5 分钟采样 → biz.oi_cvd_snapshot")
    parser.add_argument("--limit-symbols", type=int, default=0, help="只采样前 N 个符号")
    parser.add_argument("--min-vol-usd", type=float, default=0.0,
                        help="仅采样 24h 成交额 ≥ 该值（USDT）的合约（默认 0=全量）")
    parser.add_argument("--dry-run", action="store_true", help="只拉取打印，不落库")
    parser.add_argument("--watch", action="store_true", help="常驻轮询模式")
    parser.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 300）")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    if args.watch:
        print("[sampler] 常驻模式，每 %ss 一轮" % args.interval)
        while True:
            try:
                run_once(settings, args)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[warn] 本轮异常: {e}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        run_once(settings, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
