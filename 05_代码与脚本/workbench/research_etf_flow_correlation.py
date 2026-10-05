#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 资金净流 ↔ 代币价格表现 相关性投研（2026-10-03，日频，只读）。

数据：
  - biz.etf_flow_daily（source_code='farside'）：Farside 全历史
      BTC 2024-01-11 ~ 2026-10-02（700 交易日）
      ETH 2024-07-23 ~ 2026-10-02（562 交易日）
  - biz.asset_klines interval='1d'：币安 USDT 永续日频（BTCUSDT / ETHUSDT）

时间对齐纪律（无前视）：
  - flow_date 为美股交易日 T；净流于美东 T 日收盘（≈UTC T 日 20:00）后发布。
  - close(T)（UTC T+1 00:00）已包含净流发布后约 4 小时的交易，故：
      * 同期收益   ret_same = close(T+1) / close(T) - 1（含部分反应，仅描述，不作交易）
      * 可交易收益 fwd_h    = close(T+1+h) / close(T+1) - 1（T+1 收盘入场，无前视）

分析维度：
  1. 相关性：当日净流 vs 同期收益 / 后续 1/2/3/5/10 日收益（Pearson + Spearman + t）
  2. 事件桶：按净流全样本分位 5 桶（Q1 大流出 → Q5 大流入），后续收益均值/中位/胜率
  3. 极端事件：pct≥0.8 大流入 / pct≤0.2 大流出（含绝对阈值 ±300M 对照）→ 事件研究
  4. 累计净流：5/10/20 日滚动累计 vs 后续收益相关性
  5. 按年稳定性：2024 / 2025 / 2026 分年重复桶分析
  6. 长周期：累计净流 vs 价格走势（输出 CSV 供画图）

纪律：
  - 只读分析；结论是「相关性描述」，不构成阈值选型证据。
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = (1, 2, 3, 5, 10)
CUM_WINDOWS = (5, 10, 20)
EXTREME_PCT_HI = 0.80
EXTREME_PCT_LO = 0.20
ABS_EXTREME_M = 300.0      # 绝对阈值（百万 USD），与分位口径对照
MIN_N = 30
OUT_DIR = Path(__file__).resolve().parent.parent / "data"

CONTRACT = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


# ── 数据加载 ──────────────────────────────────────────────────

def load_etf_flows(conn, symbol: str) -> dict:
    """加载 Farside ETF 净流 → {date: net_flow_usd_m}。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT flow_date, net_flow_usd_m FROM biz.etf_flow_daily "
            "WHERE symbol=%s AND source_code='farside' "
            "AND net_flow_usd_m IS NOT NULL AND net_flow_usd_m <> 0 "
            "ORDER BY flow_date", (symbol,))
        for d, m in cur.fetchall():
            out[d] = float(m)
    return out


def load_klines(conn, contract: str) -> dict:
    """加载币安 1d 收盘价 → {date: close}。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, close_px FROM biz.asset_klines "
            "WHERE symbol=%s AND interval='1d' ORDER BY open_time", (contract,))
        for ot, close in cur.fetchall():
            out[ot.replace(tzinfo=None).date()] = float(close)
    return out


# ── 统计工具 ──────────────────────────────────────────────────

def pct_rank(value: float, values: list[float]) -> float:
    below = sum(1 for v in values if v <= value)
    return below / len(values)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def _spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    return _pearson(ranks(xs), ranks(ys))


def corr_stats(xs: list[float], ys: list[float]) -> dict:
    """Pearson/Spearman + 简易 t 值。"""
    n = len(xs)
    if n < MIN_N:
        return {"n": n}
    pr = _pearson(xs, ys)
    sp = _spearman(xs, ys)
    t = pr * math.sqrt(n - 2) / math.sqrt(1 - pr * pr) if abs(pr) < 1 else float("nan")
    return {"n": n, "pearson": pr, "spearman": sp, "t": t}


def stats_of(rets: list[float]) -> dict:
    if len(rets) < MIN_N:
        return {"n": len(rets)}
    mean = sum(rets) / len(rets)
    med = statistics.median(rets)
    sd = statistics.pstdev(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "median": med, "sd": sd,
            "win": win, "t": (mean / sd) * math.sqrt(len(rets)) if sd > 0 else 0.0}


# ── 序列构建 ──────────────────────────────────────────────────

def build_series(flows: dict, closes: dict) -> list[dict]:
    """对齐净流与价格，构建带同期/可交易收益与累计净流的序列。"""
    days = sorted(closes)
    day_idx = {d: i for i, d in enumerate(days)}
    n = len(days)

    # 全样本净流分位（相对自身历史）
    all_flows = [flows[d] for d in sorted(flows) if d in closes]
    series: list[dict] = []
    for d in sorted(flows):
        if d not in day_idx:
            continue
        i = day_idx[d]
        if i + 1 >= n:
            continue  # 末尾无次日收盘，跳过
        close_t = closes[d]
        close_n1 = closes[days[i + 1]]
        ret_same = close_n1 / close_t - 1.0
        fwd: dict[int, float] = {}
        for h in HORIZONS:
            if i + 1 + h < n:
                fwd[h] = closes[days[i + 1 + h]] / close_n1 - 1.0
        cum: dict[int, float] = {}
        j = i
        cnt = 0
        while j >= 0 and cnt < max(CUM_WINDOWS):
            if days[j] in flows:
                cum.setdefault(0, 0.0)
                cnt += 1
                for w in CUM_WINDOWS:
                    if cnt <= w:
                        cum[w] = cum.get(w, 0.0) + flows[days[j]]
            j -= 1
        series.append({
            "date": d, "flow_m": flows[d],
            "pct": pct_rank(flows[d], all_flows),
            "ret_same": ret_same, "fwd": fwd, "cum": cum,
        })
    return series


# ── 输出 ──────────────────────────────────────────────────────

def print_corr_table(title: str, series: list[dict], ykey: str) -> None:
    """净流 vs 各收益口径相关性。ykey: 'ret_same' 或 'fwd'。"""
    print(f"\n=== {title} ===")
    flow = [r["flow_m"] for r in series]
    labels = ["同期(T→T+1)", "T+1→T+2", "T+1→T+3", "T+1→T+4",
              "T+1→T+6", "T+1→T+11"]
    for label, h in zip(labels, ["same", 1, 2, 3, 5, 10]):
        if ykey == "ret_same" and h != "same":
            continue
        ys = [r["ret_same"] for r in series] if h == "same" else \
             [r["fwd"][h] for r in series if h in r["fwd"]]
        pairs = [(x, y) for x, y in zip(flow, ys) if y is not None and x is not None]
        xs = [p[0] for p in pairs]
        yy = [p[1] for p in pairs]
        st = corr_stats(xs, yy)
        if st["n"] < MIN_N:
            print(f"  {label:<10} n={st['n']}")
        else:
            print(f"  {label:<10} n={st['n']:<4} pearson={st['pearson']:+.3f} "
                  f"spearman={st['spearman']:+.3f} t={st['t']:+.2f}")


def print_bucket_table(series: list[dict], title: str) -> None:
    """按净流全样本分位 5 桶，展示可交易收益。"""
    print(f"\n=== {title}（可交易口径：T+1 收盘入场） ===")
    buckets: dict[str, list[dict]] = {f"Q{i}": [] for i in range(1, 6)}
    for r in series:
        q = min(5, int(r["pct"] * 5) + 1)
        buckets[f"Q{q}"].append(r)
    all_rets = {h: [r["fwd"][h] for r in series if h in r["fwd"]] for h in HORIZONS}
    baseline = {h: stats_of(all_rets[h]) for h in HORIZONS}
    print(f"  {'桶':<4} {'pct区间':<12} {'n':>4} | " +
          " | ".join(f"H{h}均/胜率" for h in HORIZONS))
    for i in range(1, 6):
        rows = buckets[f"Q{i}"]
        lo, hi = (i - 1) / 5, i / 5
        cells = []
        for h in HORIZONS:
            rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%"
                         if st["n"] >= MIN_N else f"  --/--")
        print(f"  Q{i}   [{lo:.0%},{hi:.0%})  {len(rows):>4} | " + " | ".join(cells))
    bcells = []
    for h in HORIZONS:
        st = baseline[h]
        bcells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%"
                      if st["n"] >= MIN_N else f"  --/--")
    print(f"  基线  [全样本]  {len(series):>4} | " + " | ".join(bcells))


def print_extreme_events(series: list[dict], title: str) -> None:
    """极端净流日事件研究（分位口径 + 绝对阈值口径）。"""
    print(f"\n=== {title} ===")
    for name, sel in [
        ("大流入 pct≥80%", lambda r: r["pct"] >= EXTREME_PCT_HI),
        ("大流出 pct≤20%", lambda r: r["pct"] <= EXTREME_PCT_LO),
        (f"大流入 ≥+{ABS_EXTREME_M:.0f}M", lambda r: r["flow_m"] >= ABS_EXTREME_M),
        (f"大流出 ≤-{ABS_EXTREME_M:.0f}M", lambda r: r["flow_m"] <= -ABS_EXTREME_M),
    ]:
        rows = [r for r in series if sel(r)]
        cells = []
        for h in HORIZONS:
            rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"H{h} 均={st['mean']*100:+.2f}% 胜={st['win']*100:.0f}%"
                         if st["n"] >= MIN_N else f"H{h} n={st['n']}")
        print(f"  {name:<22} n={len(rows):>3} | " + " | ".join(cells))


def print_cum_corr(series: list[dict], title: str) -> None:
    """累计净流（含当日）vs 后续可交易收益。"""
    print(f"\n=== {title} ===")
    for w in CUM_WINDOWS:
        cells = []
        for h in HORIZONS:
            pairs = [(r["cum"][w], r["fwd"][h]) for r in series
                     if h in r["fwd"] and w in r["cum"]]
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            st = corr_stats(xs, ys)
            cells.append(f"H{h}: ρ={st['pearson']:+.2f}" if st["n"] >= MIN_N else f"H{h}:--")
        print(f"  cum{w:<3} | " + " | ".join(cells))


def print_by_year(series: list[dict], title: str) -> None:
    """按年稳定性：Q1/Q5 桶的 H3/H5 平均收益。"""
    print(f"\n=== {title}（H3 / H5 平均收益 %，Q1=大流出 Q5=大流入） ===")
    years = sorted({r["date"].year for r in series})
    print(f"  {'':<10} " + "".join(f"{y:<16}" for y in years))
    for label, q in [("Q1(流出)", 1), ("Q5(流入)", 5), ("基线", None)]:
        cells = []
        for y in years:
            rows = [r for r in series if r["date"].year == y and
                    (q is None or int(r["pct"] * 5) + 1 == q)]
            out = []
            for h in (3, 5):
                rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
                st = stats_of(rets)
                out.append(f"{st['mean']*100:+.1f}" if st["n"] >= MIN_N else "--")
            cells.append(f"({out[0]}/{out[1]}) n={len(rows)}".ljust(16))
        print(f"  {label:<10} " + "".join(cells))


# ── 主流程 ──────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="ETF 净流 ↔ 价格相关性投研")
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--out-csv", default="etf_flow_price_aligned.csv")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        for symbol in args.symbols.split(","):
            symbol = symbol.strip()
            flows = load_etf_flows(conn, symbol)
            closes = load_klines(conn, CONTRACT[symbol])
            series = build_series(flows, closes)
            if not series:
                print(f"[{symbol}] 无对齐样本")
                continue
            print(f"\n########## {symbol} ETF 净流 ↔ 价格 ##########")
            print(f"  净流样本 n={len(flows)}：{min(flows)} ~ {max(flows)}")
            print(f"  对齐序列 n={len(series)}：{series[0]['date']} ~ {series[-1]['date']}")
            pos = sum(1 for r in series if r["flow_m"] > 0)
            cum = sum(r["flow_m"] for r in series)
            print(f"  流入日 {pos}/{len(series)}（{pos/len(series):.0%}）；"
                  f"累计净流 {cum:+.0f}M USD")

            print_corr_table("1. 当日净流 vs 收益相关性", series, "fwd")
            print_bucket_table(series, f"2. {symbol} 净流分位 5 桶")
            print_extreme_events(series, "3. 极端净流日事件研究（可交易）")
            print_cum_corr(series, "4. 滚动累计净流 vs 后续收益（pearson）")
            print_by_year(series, "5. 按年稳定性")

            # 输出对齐 CSV
            if args.out_csv:
                path = OUT_DIR / args.out_csv.replace(".csv", f"_{symbol}.csv")
                path.parent.mkdir(parents=True, exist_ok=True)
                fieldnames = ["date", "flow_m", "pct", "ret_same"] + \
                             [f"fwd{h}" for h in HORIZONS] + \
                             [f"cum{w}" for w in CUM_WINDOWS]
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    for r in series:
                        row = {"date": r["date"].isoformat(), "flow_m": r["flow_m"],
                               "pct": round(r["pct"], 4), "ret_same": r["ret_same"]}
                        for h in HORIZONS:
                            row[f"fwd{h}"] = round(r["fwd"][h], 6) if h in r["fwd"] else ""
                        for ww in CUM_WINDOWS:
                            row[f"cum{ww}"] = round(r["cum"][ww], 2) if ww in r["cum"] else ""
                        w.writerow(row)
                print(f"\n[CSV] 对齐数据已写: {path}")

    print("\n完成。")


if __name__ == "__main__":
    main()
