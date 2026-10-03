#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BTC/ETH 爆仓极值 → 后续价格表现回测（2026-10-03，日频，只读）。

数据（已由 phase_backfill_btc_eth_daily.py 回填）：
  - biz.asset_klines interval='1d'：币安 USDT 永续日频（BTC 2019-09-08~、ETH 2019-11-27~）
  - biz.liquidation_history interval='1d'：
      scope=binance 单所（与K线同合约同所，主口径）
      scope=all     多所聚合（2019-01 起，交叉验证口径）

信号（日 t 收盘后定义，无前视）：
  - liq_total      = long_liq + short_liq（USD，当日分段增量）
  - liq_ratio      = liq_total / quote_vol(t)（爆仓/当日成交额，相对归一化，主变量）
  - pct_liq_ratio  = liq_ratio 在「近 90 天窗口（含当日）」的分位（相对极值，非绝对阈值）
  - long_share     = long_liq / liq_total（方向结构）
  - |d1|           = 当日价格变动幅度（内生性控制变量：爆仓是被价格触发的）

事件桶：
  - EXT-ALL  ：pct_liq_ratio >= 0.90（爆仓总量极值日）
  - EXT-LONG ：pct_liq_ratio >= 0.90 且 long_share >= 0.7（多单爆仓主导 → 强制卖出）
  - EXT-SHORT：pct_liq_ratio >= 0.90 且 short_share >= 0.7（空单爆仓主导 → 强制买入）
  - 基线     ：全部样本日

结果（H = 1/3/7/14 个交易日，收盘→收盘）：
  - 平均收益、中位、胜率（正收益占比）、n、按年稳定性、日级 t 近似（均值/sqrt(n)/std）
  - 与基线对照（增量 = 事件桶 − 基线）
  - 按 |d1| 分层的条件表（内生性控制）
  - 输出极值事件清单 CSV（供新闻归因）

纪律：
  - 只读分析；不设线上阈值（沿用「标定结论 ≠ 阈值选型证据」的判读纪律）；
  - 绝对爆仓额的按年 p90 会单独输出，用以演示「绝对阈值跨 regime 不稳定」。
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import timezone
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

PCT_WINDOW = 90          # 分位计算窗口（交易日）
PCT_THR = 0.90           # 极值分位阈值
LONG_SHARE_THR = 0.70    # 方向主导阈值
HORIZONS = (1, 3, 7, 14)
CURVE_MAX = 14           # 持有期收益曲线最长 H（交易日）
MIN_N = 10               # 统计最小样本
BIN_EDGES = (0.0, 0.01, 0.03, 0.05, float("inf"))  # |d1| 分层
BIN_LABELS = ("<1%", "1-3%", "3-5%", ">5%")
OUT_DIR = Path(__file__).resolve().parent.parent / "data"


def load_daily(conn, contract: str, scope: str = "binance") -> dict:
    """加载日频K线 + 爆仓 → {date: {...}}，按日对齐。scope: binance(单所) / all(聚合)。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, open_px, close_px, quote_vol FROM biz.asset_klines "
            "WHERE symbol=%s AND interval='1d' ORDER BY open_time", (contract,))
        for ot, open_px, close, vol in cur.fetchall():
            d = ot.replace(tzinfo=None).date()
            out[d] = {"open": float(open_px or 0), "close": float(close),
                      "vol": float(vol or 0)}
        cur.execute(
            "SELECT ts, long_liq_usd, short_liq_usd FROM biz.liquidation_history "
            "WHERE symbol=%s AND interval='1d' AND exchange_scope=%s "
            "ORDER BY ts", (contract, scope))
        for ts, lon, sho in cur.fetchall():
            d = ts.replace(tzinfo=None).date()
            if d in out:
                out[d]["long_liq"] = float(lon or 0)
                out[d]["short_liq"] = float(sho or 0)
    return out


def pct_rank(value: float, window_values: list[float]) -> float:
    """value 在窗口中的经验分位（0~1）。"""
    if not window_values:
        return 0.5
    below = sum(1 for v in window_values if v <= value)
    return below / len(window_values)


def build_series(day_map: dict) -> list[dict]:
    """按日构造带特征与结果的序列（无前视）。

    每个记录带：
      - fwd[h]          ：close(t+h)/close(t)-1（信号日收盘入场）
      - fwd_open[h]     ：close(t+h)/open(t+1)-1（次日开盘入场，滑点/执行校验用）
      - open_next       ：次日开盘价（无则 None）
    """
    days = sorted(day_map)
    closes = {d: day_map[d]["close"] for d in days}
    opens = {d: day_map[d]["open"] for d in days}
    n = len(days)
    series: list[dict] = []
    for i, d in enumerate(days):
        rec = day_map[d]
        if "long_liq" not in rec:
            continue  # 缺爆仓行不参与（缺失≠0）
        liq_total = rec["long_liq"] + rec["short_liq"]
        vol = rec["vol"]
        if liq_total <= 0 or vol <= 0:
            continue
        liq_ratio = liq_total / vol
        # 当日价格变动（close(i) vs close(i-1)）
        d1 = (closes[d] / closes[days[i - 1]] - 1.0) if i >= 1 else 0.0
        # 滚动 90 日分位（含当日）
        window = [build_ratio(day_map, closes, days, j) for j in range(max(0, i - PCT_WINDOW + 1), i + 1)]
        pct = pct_rank(liq_ratio, window)
        long_share = rec["long_liq"] / liq_total
        fwd: dict[int, float] = {}
        fwd_open: dict[int, float] = {}
        for h in range(1, CURVE_MAX + 1):
            if i + h < n:
                fwd[h] = closes[days[i + h]] / closes[d] - 1.0
            if i + 1 < n and opens[days[i + 1]] > 0 and i + h < n:
                fwd_open[h] = closes[days[i + h]] / opens[days[i + 1]] - 1.0
        series.append({
            "date": d, "close": closes[d], "vol": vol,
            "open_next": opens[days[i + 1]] if i + 1 < n else None,
            "liq_total": liq_total, "liq_ratio": liq_ratio,
            "pct": pct, "long_share": long_share, "short_share": 1 - long_share,
            "d1": d1, "fwd": fwd, "fwd_open": fwd_open,
        })
    return series


def build_ratio(day_map: dict, closes: dict, days: list, idx: int) -> float:
    """当日 liq_ratio（供滚动窗口复用）。"""
    rec = day_map[days[idx]]
    if "long_liq" not in rec:
        return 0.0
    t = rec["long_liq"] + rec["short_liq"]
    v = rec["vol"]
    return t / v if v > 0 else 0.0


def bucket_of(rec: dict) -> str | None:
    """事件桶判定。"""
    if rec["pct"] >= PCT_THR:
        if rec["long_share"] >= LONG_SHARE_THR:
            return "EXT-LONG"
        if rec["short_share"] >= LONG_SHARE_THR:
            return "EXT-SHORT"
        return "EXT-MIX"
    return None


def stats_of(rows: list[dict], h: int, key: str = "fwd") -> dict:
    """一组事件的 H 日收益统计。key='fwd'=信号日收盘入场；'fwd_open'=次日开盘入场。"""
    rets = [r[key][h] for r in rows if h in r[key]]
    if len(rets) < MIN_N:
        return {"n": len(rets)}
    mean = sum(rets) / len(rets)
    med = statistics.median(rets)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {
        "n": len(rets), "mean": mean, "median": med, "sd": sd,
        "win": win, "t": (mean / sd) * math.sqrt(len(rets)) if sd > 0 else 0.0,
    }


def print_table(title: str, buckets: dict[str, list[dict]], baseline: list[dict]) -> None:
    print(f"\n=== {title} ===")
    all_rows = {"基线": baseline, **buckets}
    for name, rows in all_rows.items():
        cells = []
        for h in HORIZONS:
            st = stats_of(rows, h)
            if st["n"] < MIN_N:
                cells.append(f" n={st['n']:<4} (样本不足)")
            else:
                cells.append(
                    f" H={h:<2} n={st['n']:<4} 均={st['mean']*100:+.2f}% "
                    f"中={st['median']*100:+.2f}% 胜率={st['win']*100:.0f}% "
                    f"t={st['t']:+.2f}")
        print(f"  {name:<10}" + " | ".join(cells))


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC/ETH 爆仓极值→后续价格回测")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--pct-thr", type=float, default=PCT_THR)
    ap.add_argument("--scope", default="binance", choices=["binance", "all"],
                    help="爆仓口径：binance 单所（主）/ all 多所聚合（交叉验证）")
    ap.add_argument("--curve", action="store_true",
                    help="输出持有期收益曲线（H=1..14 平均收益）")
    ap.add_argument("--slip", action="store_true",
                    help="入场滑点校验：收盘入场 vs 次日开盘入场")
    ap.add_argument("--out-csv", default="")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        for contract in args.symbols.split(","):
            contract = contract.strip()
            day_map = load_daily(conn, contract, args.scope)
            series = build_series(day_map)
            print(f"\n########## {contract} 日频序列：n={len(series)} "
                  f"{series[0]['date']} -> {series[-1]['date']} ##########")

            # ── 0. 绝对爆仓额按年 p90（演示绝对阈值跨 regime 不稳定） ──
            by_year = defaultdict(list)
            for r in series:
                by_year[r["date"].year].append(r["liq_total"])
            print("\n=== 绝对爆仓额（binance 单所，USD）按年分布 ===")
            for y in sorted(by_year):
                vals = sorted(by_year[y])
                q90 = vals[int(0.9 * (len(vals) - 1))]
                print(f"  {y}: n={len(vals):<4} p50={statistics.median(vals)/1e6:,.1f}M "
                      f"p90={q90/1e6:,.1f}M max={vals[-1]/1e6:,.1f}M")

            # ── 1. 事件桶 ──
            buckets: dict[str, list[dict]] = {"EXT-LONG": [], "EXT-SHORT": [], "EXT-MIX": []}
            ext_events = []
            for r in series:
                b = bucket_of(r)
                if b:
                    buckets[b].append(r)
                    ext_events.append({
                        "symbol": contract, "date": r["date"].isoformat(),
                        "liq_total": r["liq_total"], "liq_ratio": r["liq_ratio"],
                        "pct": round(r["pct"], 3), "long_share": round(r["long_share"], 3),
                        "d1": r["d1"], **{f"fwd{h}": r["fwd"].get(h) for h in HORIZONS},
                    })
            print(f"\n=== 事件桶（pct≥{args.pct_thr:.0%}，方向主导=占比≥{LONG_SHARE_THR:.0%}）===")
            for b, rows in buckets.items():
                print(f"  {b}: n={len(rows)}")
            print(f"  EXT-ALL(合计): n={len(ext_events)}")

            # ── 2. 收益对照表 ──
            print_table("全样本对照", buckets, series)

            # ── 3. 内生性控制：按 |d1| 分层 ──
            print("\n=== 内生性控制：按当日 |d1| 分层（H=3 收益） ===")
            for b in ("EXT-LONG", "EXT-SHORT", "EXT-MIX", "基线"):
                rows = buckets.get(b, []) if b != "基线" else series
                cells = []
                for lo, hi, lb in zip(BIN_EDGES[:-1], BIN_EDGES[1:], BIN_LABELS):
                    sub = [r for r in rows if lo <= abs(r["d1"]) < hi]
                    st = stats_of(sub, 3)
                    if st["n"] < MIN_N:
                        cells.append(f"{lb}:n={st['n']}")
                    else:
                        cells.append(f"{lb}:n={st['n']} 均={st['mean']*100:+.2f}% "
                                     f"胜率={st['win']*100:.0f}%")
                print(f"  {b:<10} | " + " | ".join(cells))

            # ── 4. 按年稳定性（H=3） ──
            print("\n=== 按年稳定性（H=3 平均收益 %） ===")
            years = sorted({r["date"].year for r in series})
            print(f"  {'桶':<10} " + "".join(f"{y:<14}" for y in years))
            for b in ("EXT-LONG", "EXT-SHORT", "EXT-MIX", "基线"):
                rows = buckets.get(b, []) if b != "基线" else series
                yb = defaultdict(list)
                for r in rows:
                    yb[r["date"].year].append(r)
                print(f"  {b:<10} " + "".join(
                    f"{stats_of(yb[y], 3).get('mean', float('nan'))*100:+6.2f}% (n={len(yb[y])})"
                    .ljust(14) for y in years))

            # ── 4b. 持有期收益曲线（--curve） ──
            if args.curve:
                print(f"\n=== 持有期收益曲线（H=1..{CURVE_MAX} 平均收益 %，收盘入场） ===")
                print("  " + f"{'桶':<10}" + "".join(f"{h:>7}" for h in range(1, CURVE_MAX + 1)))
                for b in ("EXT-LONG", "EXT-SHORT", "EXT-MIX", "基线"):
                    rows = buckets.get(b, []) if b != "基线" else series
                    cells = []
                    for h in range(1, CURVE_MAX + 1):
                        st = stats_of(rows, h)
                        cells.append(f"{st['mean']*100:+6.2f}" if st["n"] >= MIN_N else "    -- ")
                    print(f"  {b:<10}" + "".join(f"{c:>7}" for c in cells))

            # ── 4c. 入场滑点校验（--slip） ──
            if args.slip:
                print("\n=== 入场滑点校验（H=7：收盘入场 vs 次日开盘入场） ===")
                print("  语义：信号在 t 日收盘定义。若只能次日开盘进场（真实执行），"
                      "会错过隔夜缺口（gap = open(t+1)/close(t)-1）。")
                print(f"  {'桶':<10} | {'n':<4} | {'收盘入场':>9} | {'次日开盘':>9} | "
                      f"{'差值(pp)':>9} | {'隔夜缺口p50':>9} | {'次日开盘胜率':>9}")
                for b in ("EXT-LONG", "EXT-SHORT", "EXT-MIX", "基线"):
                    rows = buckets.get(b, []) if b != "基线" else series
                    pair = [r for r in rows if 7 in r["fwd"] and 7 in r["fwd_open"]
                            and r["open_next"]]
                    if len(pair) < MIN_N:
                        print(f"  {b:<10} | 样本不足 n={len(pair)}")
                        continue
                    c_mean = sum(r["fwd"][7] for r in pair) / len(pair)
                    o_mean = sum(r["fwd_open"][7] for r in pair) / len(pair)
                    gaps = sorted(r["open_next"] / r["close"] - 1 for r in pair)
                    gap_p50 = gaps[len(gaps) // 2]
                    o_win = sum(1 for r in pair if r["fwd_open"][7] > 0) / len(pair)
                    print(f"  {b:<10} | {len(pair):<4} | {c_mean*100:+8.2f}% | "
                          f"{o_mean*100:+8.2f}% | {(c_mean-o_mean)*100:+8.2f} | "
                          f"{gap_p50*100:+8.2f}% | {o_win*100:7.0f}%")

            # ── 5. 事件清单 CSV（按 symbol 分文件，防覆盖） ──
            if args.out_csv:
                path = Path(args.out_csv.replace(".csv", f"_{contract}.csv"))
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(ext_events[0].keys()) if ext_events else [])
                    if ext_events:
                        w.writeheader()
                        w.writerows(ext_events)
                print(f"\n[CSV] 事件清单已写: {path} (n={len(ext_events)})")

    print("\n完成。")


if __name__ == "__main__":
    main()
