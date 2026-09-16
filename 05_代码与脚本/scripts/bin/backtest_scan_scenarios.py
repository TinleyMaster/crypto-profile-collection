#!/usr/bin/env python3
"""盘面异动扫描 P1 · 8 场景回测框架（新取价层，替代日线 T+N 口径）。

评审要点落地：
  - 取价层：biz.asset_klines 1h（分钟级入场：信号 bar 收盘 → 下一根开盘入场）
  - 收益口径：1h/4h/24h 三窗口，净收益 = 毛收益 − 双边 taker 手续费（0.05%×2）
  - 消融：baseline（价+量触发）vs 叠加 OI 方向的 P×OI 分桶 → 量化 OI 的边际增量
  - 横截面去相关：按入场日聚类，报告独立天数 + 日级 t 统计（保守 CI）
  - CVD 无历史源 → 8 场景中 CVD 维暂不可回测（评审 §5.1），本框架输出 P×OI 四象限，
    CVD 维待 WebSocket 流式数据积累后按同框架扩展

用法：
    python backtest_scan_scenarios.py                      # 全量回测
    python backtest_scan_scenarios.py --symbols 10         # 只测前 10 个符号
    python backtest_scan_scenarios.py --min-n 20 --cost 0.001
"""
from __future__ import annotations

import argparse
import sys
import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

PRICE_THR_1H = 3.0            # 1h 单根涨跌幅阈值（%）
VOL_RATIO_THR = 2.0           # 量 ≥ N × 近 20 根均值
LOOKBACK = 20
HORIZONS = (1, 4, 24)         # 持有小时数
COST = 0.001                  # 双边 taker 手续费（0.05%×2=0.1%）
MIN_N = 20                    # 桶最少样本数
MIN_DAYS = 5                  # 最少独立天数
MIN_KLINES_BARS = 1000        # 只测有足够 1h 历史的符号

SCENARIOS = ("Pup_OIup", "Pup_OIdown", "Pdown_OIup", "Pdown_OIdown")


def load_klines(conn, symbols: list[str]) -> dict[str, list[dict]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT symbol, open_time, open_px, close_px, quote_vol FROM biz.asset_klines "
            "WHERE interval='1h' AND symbol = ANY(%s) ORDER BY symbol, open_time",
            (symbols,),
        )
        rows = cur.fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["symbol"]].append({
            "t": r["open_time"], "open": float(r["open_px"]),
            "close": float(r["close_px"]), "vol": float(r["quote_vol"] or 0),
        })
    return dict(out)


def load_oi_hourly(conn, symbols: list[str]) -> dict[str, dict[datetime, float]]:
    """小时级 OI（回填 1h 点 + 实时 5m 桶聚合），返回 {symbol: {hour_ts: oi}}。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT symbol, date_trunc('hour', ts) AS h, AVG(oi_usd) AS oi "
            "FROM biz.oi_cvd_snapshot WHERE oi_usd IS NOT NULL AND symbol = ANY(%s) "
            "GROUP BY symbol, date_trunc('hour', ts)",
            (symbols,),
        )
        rows = cur.fetchall()
    out: dict[str, dict[datetime, float]] = defaultdict(dict)
    for r in rows:
        if r["oi"] is not None:
            out[r["symbol"]][r["h"]] = float(r["oi"])
    return dict(out)


def hour_key(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def scan_symbol(bars: list[dict], oi_hours: dict[datetime, float],
                trades: dict[str, list], cost: float) -> None:
    """扫描单符号，产出 (scenario, horizon, day, net_ret) 记录。"""
    n = len(bars)
    max_h = max(HORIZONS)
    for t in range(LOOKBACK + 1, n - max_h - 1):
        bar = bars[t]
        prev = bars[t - 1]
        if not prev["close"]:
            continue
        chg = (bar["close"] - prev["close"]) / prev["close"] * 100
        vols = [b["vol"] for b in bars[t - LOOKBACK:t]]
        vol_mean = sum(vols) / LOOKBACK if vols else 0
        vol_ratio = bar["vol"] / vol_mean if vol_mean else 0.0
        if abs(chg) < PRICE_THR_1H or vol_ratio < VOL_RATIO_THR:
            continue
        direction = "up" if chg >= 0 else "down"

        # OI 方向（可缺失 → 基线样本，用于消融）
        h = hour_key(bar["t"])
        oi_now = oi_hours.get(h)
        oi_prev = oi_hours.get(h - timedelta(hours=1))
        if oi_now is not None and oi_prev is not None and oi_prev != 0:
            oi_dir = "up" if oi_now > oi_prev else "down"
            scenario = f"P{direction}_OI{oi_dir}"
        else:
            scenario = "BASELINE_ONLY"

        entry = bars[t + 1]["open"]
        if not entry:
            continue
        day = bars[t + 1]["t"].date()
        for hz in HORIZONS:
            exit_close = bars[t + hz]["close"]
            if not exit_close:
                continue
            ret_long = (exit_close - entry) / entry
            ret = ret_long if direction == "up" else -ret_long
            trades[scenario].append((hz, day, ret - cost))


def summarize(trades: dict[str, list], min_n: int, min_days: int) -> list[dict]:
    """按 (scenario, horizon) 统计，含日级聚类去相关。"""
    rows: list[dict] = []
    for scenario, recs in trades.items():
        for hz in HORIZONS:
            sub = [r for r in recs if r[0] == hz]
            if len(sub) < min_n:
                continue
            nets = [r[2] for r in sub]
            days = {r[1] for r in sub}
            if len(days) < min_days:
                continue
            wins = sum(1 for x in nets if x > 0)
            gross_win = sum(x for x in nets if x > 0)
            gross_loss = abs(sum(x for x in nets if x < 0))
            # 日级聚类
            day_means: dict = defaultdict(list)
            for r in sub:
                day_means[r[1]].append(r[2])
            dm = [sum(v) / len(v) for v in day_means.values()]
            day_mean = sum(dm) / len(dm)
            day_std = (sum((x - day_mean) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5 if len(dm) > 1 else 0.0
            t_stat = day_mean / (day_std / (len(dm) ** 0.5)) if day_std > 0 else 0.0
            rows.append({
                "scenario": scenario, "horizon_h": hz, "n": len(sub),
                "days": len(dm), "win_rate": wins / len(sub),
                "avg_ret_net": sum(nets) / len(nets), "expectancy": sum(nets) / len(sub),
                "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
                "day_t_stat": t_stat, "day_avg": day_mean,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="8 场景回测（1h 取价 + 成本 + 消融 + 日级聚类）")
    parser.add_argument("--symbols", type=int, default=0, help="只测前 N 个符号")
    parser.add_argument("--cost", type=float, default=COST, help="双边手续费（默认 0.001）")
    parser.add_argument("--min-n", type=int, default=MIN_N)
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径（默认 scripts/data/backtest_scan_results.csv）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, COUNT(*) AS n FROM biz.asset_klines "
                "WHERE interval='1h' GROUP BY symbol HAVING COUNT(*) >= %s ORDER BY symbol",
                (MIN_KLINES_BARS,),
            )
            universe = [r[0] for r in cur.fetchall()]
        if args.symbols:
            universe = universe[: args.symbols]
        print(f"[backtest] 符号宇宙 {len(universe)}，1h 窗口 {HORIZONS}h，成本 {args.cost:.3f}")

        klines = load_klines(conn, universe)
        oi_hourly = load_oi_hourly(conn, universe)
        print(f"[backtest] K线 {sum(len(v) for v in klines.values())} 根；"
              f"OI 小时序列 {sum(len(v) for v in oi_hourly.values())} 点")

        trades: dict[str, list] = defaultdict(list)
        for sym in universe:
            scan_symbol(klines.get(sym, []), oi_hourly.get(sym, {}), trades, args.cost)

        rows = summarize(trades, args.min_n, MIN_DAYS)
        print(f"\n{'场景':<16}{'窗口h':>5}{'n':>6}{'天数':>5}{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
        print("-" * 76)
        for r in sorted(rows, key=lambda x: (x["scenario"], x["horizon_h"])):
            print(f"{r['scenario']:<16}{r['horizon_h']:>5}{r['n']:>6}{r['days']:>5}"
                  f"{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>10.3f}"
                  f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                  f"{r['day_t_stat']:>8.2f}")

        # 消融对比：baseline vs 各 P×OI 桶
        base = next((r for r in rows if r["scenario"] == "BASELINE_ONLY" and r["horizon_h"] == 1), None)
        if base:
            print(f"\n=== 消融（1h 窗口，baseline 净均={base['avg_ret_net'] * 100:.3f}%）===")
            for r in rows:
                if r["scenario"] == "BASELINE_ONLY":
                    continue
                delta = (r["avg_ret_net"] - base["avg_ret_net"]) * 100
                print(f"  {r['scenario']:<16} n={r['n']:<6} 净均={r['avg_ret_net'] * 100:>7.3f}%  "
                      f"Δvs基线={delta:>+7.3f}%")

        if not args.out:
            args.out = str(SCRIPT_DIR.parent / "data" / "backtest_scan_results.csv")
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["scenario"])
            w.writeheader()
            w.writerows(rows)
        print(f"\n[backtest] 结果已存 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
