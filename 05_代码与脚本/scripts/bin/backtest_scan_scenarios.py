#!/usr/bin/env python3
"""盘面异动扫描 P1 · 8 场景回测框架（新取价层，替代日线 T+N 口径）。

评审要点落地：
  - 取价层：biz.asset_klines 1h（分钟级入场：信号 bar 收盘 → 下一根开盘入场）
  - 收益口径：1h/4h/24h 三窗口，净收益 = 毛收益 − 双边 taker 手续费（0.05%×2）
  - 消融：baseline（价+量触发）vs 叠加 OI 方向的 P×OI 分桶 → 量化 OI 的边际增量
  - 横截面去相关：按入场日聚类，报告独立天数 + 日级 t 统计（保守 CI）
  - CVD 无历史源 → 8 场景中 CVD 维暂不可回测（评审 §5.1），本框架输出 P×OI 四象限，
    CVD 维待 WebSocket 流式数据积累后按同框架扩展
  - funding 消融（历史已回填 biz.funding_rate_hist）：每个 P×OI 场景再按结算点资金费率
    正/负/近零拆分 → 验证"高费率做多更差"假设，评估拥挤度过滤是否值得加进 L2 校验

用法：
    python backtest_scan_scenarios.py                      # 全量回测
    python backtest_scan_scenarios.py --symbols 10         # 只测前 10 个符号
    python backtest_scan_scenarios.py --min-n 20 --cost 0.001
"""
from __future__ import annotations

import argparse
import sys
import csv
import bisect
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

PRICE_THR_1H = 4.0            # 1h 单根涨跌幅阈值（%），2026-09-17 阈值敏感性标定：高确定性定位 3.0→4.0
VOL_RATIO_THR = 2.0           # 量 ≥ N × 近 20 根均值
LOOKBACK = 20
HORIZONS = (1, 4, 24)         # 持有小时数
COST = 0.001                  # 双边 taker 手续费（0.05%×2=0.1%）
MIN_N = 20                    # 桶最少样本数
MIN_DAYS = 5                  # 最少独立天数
MIN_KLINES_BARS = 1000        # 只测有足够 1h 历史的符号
FUND_POS_THR = 0.0001         # 资金费率正负判定阈值（±1bp/8h）

SCENARIOS = ("Pup_OIup", "Pup_OIdown", "Pdown_OIup", "Pdown_OIdown")


def load_klines(conn, symbols: list[str], lookback_days: int = 0) -> dict[str, list[dict]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if lookback_days > 0:
            cur.execute(
                "SELECT symbol, open_time, open_px, close_px, quote_vol FROM biz.asset_klines "
                "WHERE interval='1h' AND symbol = ANY(%s) "
                "AND open_time >= NOW() - make_interval(days => %s) "
                "ORDER BY symbol, open_time",
                (symbols, lookback_days),
            )
        else:
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


def load_funding(conn, symbols: list[str]) -> dict[str, tuple[list, list[float]]]:
    """资金费率历史（8h 结算点）→ {symbol: (sorted_fts, rates)}，供 bisect 近邻查找。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT symbol, funding_time, rate FROM biz.funding_rate_hist "
            "WHERE symbol = ANY(%s) ORDER BY symbol, funding_time",
            (symbols,),
        )
        rows = cur.fetchall()
    out: dict[str, tuple[list, list[float]]] = defaultdict(lambda: ([], []))
    for r in rows:
        if r["rate"] is not None:
            out[r["symbol"]][0].append(r["funding_time"])
            out[r["symbol"]][1].append(float(r["rate"]))
    return {k: v for k, v in out.items() if v[0]}


def funding_tag(funding: tuple[list, list[float]] | None, h: datetime) -> str | None:
    """取 <= h 的最近结算点费率，映射为 '+'（正）/ '-'（负）/ '0'（近零）；无数据返回 None。"""
    if not funding:
        return None
    fts, rates = funding
    i = bisect.bisect_right(fts, h) - 1
    if i < 0:
        return None
    rate = rates[i]
    if rate > FUND_POS_THR:
        return "+"
    if rate < -FUND_POS_THR:
        return "-"
    return "0"


def hour_key(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def scan_symbol(bars: list[dict], oi_hours: dict[datetime, float],
                funding: tuple[list, list[float]] | None,
                trades: dict[str, list], cost: float,
                price_thr: float = PRICE_THR_1H,
                vol_thr: float = VOL_RATIO_THR) -> None:
    """扫描单符号，产出 (scenario, horizon, day, net_ret, fund_tag) 记录。"""
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
        if abs(chg) < price_thr or vol_ratio < vol_thr:
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
        ftag = funding_tag(funding, h) or "NA"

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
            trades[scenario].append((hz, day, ret - cost, ftag))


def sweep_all(klines: dict[str, list], oi_hourly: dict[str, dict[datetime, float]],
              funding: dict[str, tuple[list, list[float]]], cost: float,
              price_thrs: list[float], vol_thrs: list[float],
              min_n: int, min_days: int) -> list[dict]:
    """阈值敏感性扫描：对每组 (price_thr, vol_thr) 跑全宇宙，聚焦 P↑OI↑ 场景。

    返回行：{price_thr, vol_thr, scenario, horizon, n, days, win_rate,
             avg_ret_net, profit_factor, day_t_stat}。
    """
    rows: list[dict] = []
    for pt in price_thrs:
        for vt in vol_thrs:
            trades: dict[str, list] = defaultdict(list)
            for sym in klines:
                scan_symbol(klines[sym], oi_hourly.get(sym, {}),
                            funding.get(sym), trades, cost, price_thr=pt, vol_thr=vt)
            for r in summarize(trades, min_n, min_days):
                if r["scenario"] not in ("Pup_OIup", "Pup_OIdown"):
                    continue
                r = dict(r)
                r["price_thr"] = pt
                r["vol_thr"] = vt
                rows.append(r)
    return rows


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


def summarize_funding(trades: dict[str, list], min_n: int, min_days: int) -> list[dict]:
    """funding 消融：每个 (scenario, horizon, fund_tag) 分桶统计。"""
    rows: list[dict] = []
    for scenario, recs in trades.items():
        tags = {r[3] for r in recs}
        for ftag in sorted(tags):
            for hz in HORIZONS:
                sub = [r for r in recs if r[0] == hz and r[3] == ftag]
                if len(sub) < min_n:
                    continue
                nets = [r[2] for r in sub]
                days = {r[1] for r in sub}
                if len(days) < min_days:
                    continue
                wins = sum(1 for x in nets if x > 0)
                gross_win = sum(x for x in nets if x > 0)
                gross_loss = abs(sum(x for x in nets if x < 0))
                day_means: dict = defaultdict(list)
                for r in sub:
                    day_means[r[1]].append(r[2])
                dm = [sum(v) / len(v) for v in day_means.values()]
                day_mean = sum(dm) / len(dm)
                day_std = (sum((x - day_mean) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5 if len(dm) > 1 else 0.0
                t_stat = day_mean / (day_std / (len(dm) ** 0.5)) if day_std > 0 else 0.0
                rows.append({
                    "scenario": scenario, "fund_tag": ftag, "horizon_h": hz, "n": len(sub),
                    "days": len(dm), "win_rate": wins / len(sub),
                    "avg_ret_net": sum(nets) / len(nets), "expectancy": sum(nets) / len(sub),
                    "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
                    "day_t_stat": t_stat, "day_avg": day_mean,
                })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="8 场景回测（1h 取价 + 成本 + 消融 + 日级聚类 + 阈值敏感性扫描）")
    parser.add_argument("--symbols", type=int, default=0, help="只测前 N 个符号")
    parser.add_argument("--cost", type=float, default=COST, help="双边手续费（默认 0.001）")
    parser.add_argument("--min-n", type=int, default=MIN_N)
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径（默认 scripts/data/backtest_scan_results.csv）")
    parser.add_argument("--sweep", action="store_true",
                        help="阈值敏感性扫描模式：对价格/量比阈值网格跑 P↑OI↑，输出矩阵 CSV")
    parser.add_argument("--price-thrs", type=str, default="2.0,2.5,3.0,3.5,4.5,6.0",
                        help="扫描的价格阈值列表（逗号分隔，默认 2.0,2.5,3.0,3.5,4.5,6.0）")
    parser.add_argument("--vol-thrs", type=str, default="1.5,2.0,3.0,4.0",
                        help="扫描的量比阈值列表（逗号分隔，默认 1.5,2.0,3.0,4.0）")
    parser.add_argument("--lookback-days", type=int, default=0,
                        help="只加载最近 N 天 1h K 线（0=全量；回测建议 45：覆盖 30 天 OI + 缓冲）")
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

        klines = load_klines(conn, universe, args.lookback_days)
        oi_hourly = load_oi_hourly(conn, universe)
        funding = load_funding(conn, universe)
        print(f"[backtest] K线 {sum(len(v) for v in klines.values())} 根；"
              f"OI 小时序列 {sum(len(v) for v in oi_hourly.values())} 点；"
              f"funding 序列 {sum(len(v[0]) for v in funding.values())} 点")

        if args.sweep:
            pt_list = [float(x) for x in args.price_thrs.split(",") if x.strip()]
            vt_list = [float(x) for x in args.vol_thrs.split(",") if x.strip()]
            print(f"[sweep] 阈值网格 {len(pt_list)}×{len(vt_list)}={len(pt_list) * len(vt_list)} 组，"
                  f"聚焦 P↑OI↑ / P↑OI↓")
            srows = sweep_all(klines, oi_hourly, funding, args.cost,
                              pt_list, vt_list, args.min_n, MIN_DAYS)
            print(f"\n{'价格阈值':>6}{'量比阈值':>6}{'场景':<10}{'窗口h':>5}{'n':>6}"
                  f"{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
            print("-" * 76)
            for r in sorted(srows, key=lambda x: (x["price_thr"], x["vol_thr"],
                                                  x["scenario"], x["horizon_h"])):
                print(f"{r['price_thr']:>6.1f}{r['vol_thr']:>6.1f}{r['scenario']:<10}"
                      f"{r['horizon_h']:>5}{r['n']:>6}{r['win_rate']:>8.1%}"
                      f"{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                      f"{r['day_t_stat']:>8.2f}")
            if srows:
                out_path = Path(args.out) if args.out else \
                    SCRIPT_DIR.parent / "data" / "backtest_threshold_sweep.csv"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(srows[0].keys()))
                    w.writeheader()
                    w.writerows(srows)
                print(f"\n[sweep] 矩阵已存 {out_path}")
            return 0

        trades: dict[str, list] = defaultdict(list)
        for sym in universe:
            scan_symbol(klines.get(sym, []), oi_hourly.get(sym, {}),
                        funding.get(sym), trades, args.cost)

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

        # funding 消融：按资金费率正/负/近零拆开
        frows = summarize_funding(trades, args.min_n, MIN_DAYS)
        if frows:
            print(f"\n=== funding 消融（资金费率: + 正 / - 负 / 0 近零 / NA 无数据）===")
            print(f"{'场景':<16}{'费率':>5}{'窗口h':>5}{'n':>6}{'天数':>5}{'胜率':>8}{'净均收益%':>10}{'盈亏比':>8}{'日t值':>8}")
            print("-" * 84)
            for r in sorted(frows, key=lambda x: (x["scenario"], x["horizon_h"], x["fund_tag"])):
                print(f"{r['scenario']:<16}{r['fund_tag']:>5}{r['horizon_h']:>5}{r['n']:>6}{r['days']:>5}"
                      f"{r['win_rate']:>8.1%}{r['avg_ret_net'] * 100:>10.3f}"
                      f"{r['profit_factor'] if r['profit_factor'] != float('inf') else 999:>8.2f}"
                      f"{r['day_t_stat']:>8.2f}")

        if not args.out:
            args.out = str(SCRIPT_DIR.parent / "data" / "backtest_scan_results.csv")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["scenario"])
            w.writeheader()
            w.writerows(rows)
        print(f"\n[backtest] 结果已存 {args.out}")

        if frows:
            fout = out_path.with_name("backtest_funding_ablation.csv")
            with open(fout, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(frows[0].keys()))
                w.writeheader()
                w.writerows(frows)
            print(f"[backtest] funding 消融已存 {fout}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
