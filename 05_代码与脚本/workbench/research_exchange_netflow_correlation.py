#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交易所净流（链上 onchain）↔ 代币价格表现 相关性投研（面板，只读，2026-10-04）。

数据：
  - biz.onchain_netflow_hourly：资产×小时交易所净流因子（2026-08-01 ~ 2026-10-04）
      netflow = inflow - outflow（正=净流入交易所=潜在抛压/看空；负=净流出=吸筹/看多）
      inflow = 转入交易所钱包 USD；outflow = 提币离场 USD
  - biz.v_asset_market_daily_primary：资产日行情单源视图（cmc>cmc_historical>binance_klines）
  - core.asset：symbol / canonical_name / market_cap_rank

口径与纪律：
  - 日净流 = 当日（UTC）各小时 netflow_usd 求和；仅统计有链上归因的转移
  - 日收益 = 日线 price_usd 环比（market_daily，剔除 is_anomaly 行）
  - 归一化：net_ratio = 日净流 / 当日 market_cap（跨资产可比，单位=市值占比）
  - 样本过滤：净流覆盖 ≥ 20 个自然日；价格覆盖 ≥ 20 日；剔除稳定币/低波动资产
    （日 |收益| 中位数 < 0.3%）；剔除 market_cap_rank 缺失的资产
  - 前瞻收益：fwd_h = close(T+h)/close(T) - 1，h=1/2/3/5，无前视
  - 超额收益：asset_ret - BTC_ret（同期，控制市场 beta）作为稳健性对照

分析维度：
  1. 面板相关：日净流（原始 USD / 市值归一化） vs 同期 + 前瞻 1/2/3/5 日收益
     （Pearson + Spearman + 近似 t；面板 t 仅作参考，结论以资产内相关为准）
  2. 资产内时间序列相关：每资产 Pearson(flow, ret_{T+k})，跨资产聚合均值/中位/正负占比 + t
  3. 分位 5 桶：net_ratio 全面板分位（Q1 大流出 → Q5 大流入）前瞻收益均值/胜率 vs 基线
  4. 极端事件：pct≥80% 大流入 / pct≤20% 大流出 → 事件研究（H1/H3/H5）
  5. 累计净流：5/10 日滚动累计 net_ratio vs 前瞻收益（慢变量检验）
  6. 稳健性：超额收益（vs BTC）重复相关 + 桶分析
  7. 输出对齐面板 CSV（data/exchange_netflow_price_panel.csv）

结论解读（重要）：
  净流符号 = inflow - outflow。若「流入交易所=抛压」假设成立，
  净流与前瞻收益应为**负相关**；若呈现正相关则为均值回归/反向。
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = (1, 2, 3, 5)
CUM_WINDOWS = (5, 10)
EXTREME_PCT_HI = 0.80
EXTREME_PCT_LO = 0.20
MIN_N = 30          # 统计量最小样本（面板口径）
MIN_ASSET_N = 8     # 资产内相关聚合时，单资产最少配对日
MIN_DAYS = 20       # 单资产最少净流/价格覆盖天数
MIN_MED_ABS_RET = 0.003  # 稳定币/低波动过滤：日|收益|中位数下限
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
WINDOW_START = "2026-08-01"


# ── 数据加载 ──────────────────────────────────────────────────

def load_daily_netflow(conn) -> list[tuple]:
    """加载资产×日净流：(asset_id, date, inflow, outflow, netflow)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, bucket_hour::date AS d,
                   SUM(inflow_usd) AS inflow, SUM(outflow_usd) AS outflow,
                   SUM(netflow_usd) AS netflow
            FROM biz.onchain_netflow_hourly
            WHERE bucket_hour >= %s::date
            GROUP BY 1, 2
            ORDER BY 1, 2
            """, (WINDOW_START,))
        return cur.fetchall()


def load_market_daily(conn, asset_ids: set[int]) -> dict[int, dict]:
    """加载资产×日行情 → {asset_id: {date: (price, market_cap)}}。

    直查基表（用 PK 索引过滤），Python 端按源优先级去重：cmc > cmc_historical > binance_klines，
    避免对全表做 DISTINCT ON 的视图在远端慢/断连。
    """
    src_prio = {"cmc": 0, "cmc_historical": 1, "binance_klines": 2}
    # asset_id -> date -> (prio, price, market_cap)
    best: dict[int, dict] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, market_date, source_code, price_usd, market_cap
            FROM biz.asset_market_daily
            WHERE market_date >= %s::date
              AND asset_id = ANY(%s::int[])
              AND price_usd IS NOT NULL AND price_usd > 0
              AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
            ORDER BY asset_id, market_date
            """, (WINDOW_START, list(asset_ids)))
        for aid, d, src, price, mc in cur.fetchall():
            prio = src_prio.get(src, 9)
            cur_best = best[aid].get(d)
            if cur_best is None or prio < cur_best[0]:
                best[aid][d] = (prio, float(price), float(mc) if mc else None)
    return {aid: {d: (v[1], v[2]) for d, v in days.items()} for aid, days in best.items()}


def load_asset_meta(conn, asset_ids: set[int]) -> dict[int, dict]:
    """加载资产元数据：symbol / name / market_cap_rank（仅净流资产）。"""
    out: dict[int, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id, canonical_symbol, canonical_name, market_cap_rank "
            "FROM core.asset WHERE asset_id = ANY(%s::int[])",
            (list(asset_ids),))
        for aid, sym, name, rank in cur.fetchall():
            out[aid] = {"symbol": str(sym), "name": str(name or ""), "rank": rank}
    return out


# ── 统计工具 ──────────────────────────────────────────────────

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


def corr_stats(xs: list[float], ys: list[float], min_n: int = MIN_N) -> dict:
    n = len(xs)
    if n < min_n:
        return {"n": n}
    pr = _pearson(xs, ys)
    sp = _spearman(xs, ys)
    t = pr * math.sqrt(n - 2) / math.sqrt(1 - pr * pr) if abs(pr) < 1 else float("nan")
    return {"n": n, "pearson": pr, "spearman": sp, "t": t}


def stats_of(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    mean = sum(rets) / len(rets)
    med = statistics.median(rets)
    sd = statistics.pstdev(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "median": med, "sd": sd, "win": win}


def one_sample_t(values: list[float]) -> dict:
    """对跨资产聚合的 rho 做单样本 t 检验（每资产一个观测）。"""
    n = len(values)
    if n < 2:
        return {"n": n}
    mean = sum(values) / n
    sd = statistics.stdev(values)
    if sd == 0:
        return {"n": n, "mean": mean, "median": statistics.median(values), "t": float("nan")}
    return {"n": n, "mean": mean, "median": statistics.median(values),
            "pos_share": sum(1 for v in values if v > 0) / n, "t": mean / (sd / math.sqrt(n))}


def pct_rank(value: float, values: list[float]) -> float:
    below = sum(1 for v in values if v <= value)
    return below / len(values)


# ── 面板构建 ──────────────────────────────────────────────────

def build_panel(netflow_rows: list, market: dict[int, dict], meta: dict[int, dict],
                min_days: int = MIN_DAYS, min_med_abs_ret: float = MIN_MED_ABS_RET) -> tuple[list[dict], dict[int, list[float]]]:
    """构建面板：每行 = 资产×日（含日净流、归一化、同期/前瞻收益、累计净流）。

    返回 (panel, per_asset_median_abs_ret)。
    """
    # 先按资产聚合日序列（净流只取有覆盖的日）
    flow_by_asset: dict[int, dict] = defaultdict(dict)   # asset_id -> {date: netflow_usd}
    inflow_by_asset: dict[int, dict] = defaultdict(dict)
    outflow_by_asset: dict[int, dict] = defaultdict(dict)
    for aid, d, fin, fout, fnet in netflow_rows:
        flow_by_asset[aid][d] = float(fnet)
        inflow_by_asset[aid][d] = float(fin)
        outflow_by_asset[aid][d] = float(fout)

    # BTC 当日/前瞻收益（超额收益基准）
    btc_fwd: dict[int, dict] = {}
    btc_day: dict = {}
    btc_id = next((aid for aid, m in meta.items() if m["symbol"] == "BTC"), None)
    if btc_id is not None:
        bdates = sorted(market.get(btc_id, {}))
        bn = len(bdates)
        for i in range(1, bn):
            btc_day[bdates[i]] = market[btc_id][bdates[i]][0] / market[btc_id][bdates[i - 1]][0] - 1.0
        for i, d in enumerate(bdates):
            btc_fwd[d] = {}
            for h in HORIZONS:
                if i + h < bn:
                    btc_fwd[d][h] = market[btc_id][bdates[i + h]][0] / market[btc_id][bdates[i]][0] - 1.0

    panel: list[dict] = []
    per_asset_mar: dict[int, list[float]] = {}

    for aid, flows in flow_by_asset.items():
        meta_a = meta.get(aid)
        if not meta_a or meta_a["rank"] is None:
            continue
        if len(flows) < min_days:
            continue
        px = market.get(aid)
        if not px or len(px) < min_days:
            continue
        # 日收益序列（连续日）
        dates = sorted(px)
        idx = {d: i for i, d in enumerate(dates)}
        rets = {}
        for i in range(1, len(dates)):
            rets[dates[i]] = px[dates[i]][0] / px[dates[i - 1]][0] - 1.0
        # 稳定币/低波动过滤
        med_abs = statistics.median([abs(v) for v in rets.values() if v is not None]) if rets else 0.0
        per_asset_mar[aid] = [abs(v) for v in rets.values()]
        if med_abs < min_med_abs_ret:
            continue

        n = len(dates)
        for d, fnet in flows.items():
            if d not in px:
                continue
            i = idx[d]
            if i + 1 >= n:
                continue
            ret_day = px[dates[i]][0] / px[dates[i - 1]][0] - 1.0 if i >= 1 else 0.0
            close_t = px[d][0]
            close_n1 = px[dates[i + 1]][0]
            if close_t <= 0 or close_n1 <= 0:
                continue
            fwd: dict[int, float] = {}
            for h in HORIZONS:
                if i + h < n:
                    fwd[h] = px[dates[i + h]][0] / close_t - 1.0
            mc = px[d][1]
            if not mc or mc <= 0:
                continue
            net_ratio = fnet / mc
            tot = inflow_by_asset[aid].get(d, 0.0) + outflow_by_asset[aid].get(d, 0.0)
            net_share = fnet / tot if tot > 0 else float("nan")
            # 累计净流（含当日，回看 CUM_WINDOWS）
            cum: dict[int, float] = {}
            j = i
            cnt = 0
            while j >= 0 and cnt < max(CUM_WINDOWS):
                if dates[j] in flows:
                    cnt += 1
                    for w in CUM_WINDOWS:
                        if cnt <= w:
                            cum[w] = cum.get(w, 0.0) + flows[dates[j]] / (px[dates[j]][1] or mc)
                j -= 1
            panel.append({
                "asset_id": aid, "symbol": meta_a["symbol"], "name": meta_a["name"],
                "date": d, "netflow_usd": fnet, "net_ratio": net_ratio,
                "net_share": net_share, "ret_day": ret_day, "fwd": fwd, "cum": cum,
                "btc_fwd": btc_fwd.get(d, {}), "btc_day": btc_day.get(d, 0.0),
            })

    return panel, per_asset_mar


def excess_ret(row: dict, h: int) -> float:
    """资产 h 日收益 − BTC 同期 h 日收益（同一入场日，控制市场 beta）。"""
    base = row["fwd"].get(h)
    btc = row["btc_fwd"].get(h)
    if base is None or btc is None:
        return float("nan")
    return base - btc


# ── 输出 ──────────────────────────────────────────────────────

def print_panel_corr(panel: list[dict], title: str, use_excess: bool = False) -> None:
    print(f"\n=== {title} ===")
    flow_raw = [r["netflow_usd"] for r in panel]
    flow_ratio = [r["net_ratio"] for r in panel]
    labels = ["同期(T日)", "前瞻 T→T+1", "前瞻 T→T+2", "前瞻 T→T+3", "前瞻 T→T+5"]
    for label, h in zip(labels, ["same", 1, 2, 3, 5]):
        if use_excess:
            ys = [excess_ret(r, h) for r in panel] if h != "same" else \
                 [r["ret_day"] - r["btc_day"] for r in panel]
        else:
            ys = [r["fwd"][h] for r in panel if h in r["fwd"]] if h != "same" else \
                 [r["ret_day"] for r in panel]
        # 原始 USD
        pairs = [(x, y) for x, y in zip(flow_raw, ys) if y is not None and not math.isnan(y)]
        st_raw = corr_stats([p[0] for p in pairs], [p[1] for p in pairs])
        # 市值归一化
        pairs2 = [(x, y) for x, y in zip(flow_ratio, ys) if y is not None and not math.isnan(y)]
        st_ratio = corr_stats([p[0] for p in pairs2], [p[1] for p in pairs2])
        if st_raw["n"] < MIN_N:
            print(f"  {label:<12} n={st_raw['n']}")
        else:
            print(f"  {label:<12} n={st_raw['n']:<5} USD: ρ={st_raw['pearson']:+.3f}"
                  f" (t={st_raw['t']:+.2f}) | 市值归一: ρ={st_ratio['pearson']:+.3f}"
                  f" (t={st_ratio['t']:+.2f}) spearman={st_ratio['spearman']:+.3f}")


def print_per_asset_corr(panel: list[dict], title: str, use_excess: bool = False) -> None:
    print(f"\n=== {title} ===")
    by_asset: dict[int, list[dict]] = defaultdict(list)
    for r in panel:
        by_asset[r["asset_id"]].append(r)
    labels = ["同期(T日)", "前瞻 T→T+1", "前瞻 T→T+2", "前瞻 T→T+3", "前瞻 T→T+5"]
    print(f"  {'口径':<12} {'资产数':>5} | 平均ρ | 中位ρ | ρ>0占比 | t")
    for label, h in zip(labels, ["same", 1, 2, 3, 5]):
        rhos: list[float] = []
        for _aid, rows in by_asset.items():
            pairs = []
            for r in rows:
                if h == "same":
                    y = r["ret_day"] - r["btc_day"] if use_excess else r["ret_day"]
                else:
                    y = excess_ret(r, h) if use_excess else r["fwd"].get(h)
                if y is not None and not math.isnan(y) and not math.isnan(r["net_ratio"]):
                    pairs.append((r["net_ratio"], y))
            if len(pairs) >= MIN_ASSET_N:
                rhos.append(_pearson([p[0] for p in pairs], [p[1] for p in pairs]))
        rhos = [x for x in rhos if not math.isnan(x)]
        st = one_sample_t(rhos)
        if st["n"] < 5:
            print(f"  {label:<12} {st['n']:>5}  | 样本不足")
        else:
            print(f"  {label:<12} {st['n']:>5}  | {st['mean']:+.3f}  | {st['median']:+.3f}"
                  f"  | {st['pos_share']:>5.0%}   | {st['t']:+.2f}")


def print_bucket_table(panel: list[dict], title: str, use_excess: bool = False) -> None:
    print(f"\n=== {title} ===")
    ratios = [r["net_ratio"] for r in panel]
    buckets: dict[str, list[dict]] = {f"Q{i}": [] for i in range(1, 6)}
    for r in panel:
        q = min(5, int(pct_rank(r["net_ratio"], ratios) * 5) + 1)
        buckets[f"Q{q}"].append(r)
    all_rets = {h: [r["fwd"][h] for r in panel if h in r["fwd"]] for h in HORIZONS}
    if use_excess:
        all_rets = {h: [excess_ret(r, h) for r in panel
                        if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
                    for h in HORIZONS}
    baseline = {h: stats_of(all_rets[h]) for h in HORIZONS}
    print(f"  {'桶':<4} {'净流占比区间':<12} {'n':>5} | " + " | ".join(f"H{h}均/胜" for h in HORIZONS))
    for i in range(1, 6):
        rows = buckets[f"Q{i}"]
        lo, hi = (i - 1) / 5, i / 5
        cells = []
        for h in HORIZONS:
            if use_excess:
                rets = [excess_ret(r, h) for r in rows if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
            else:
                rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else f"  --/--")
        print(f"  Q{i}   [{lo:.0%},{hi:.0%})  {len(rows):>5} | " + " | ".join(cells))
    bcells = []
    for h in HORIZONS:
        st = baseline[h]
        bcells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else f"  --/--")
    print(f"  基线  [全样本]  {len(panel):>5} | " + " | ".join(bcells))


def print_extreme_events(panel: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    ratios = [r["net_ratio"] for r in panel]
    for name, sel in [
        ("大流出(吸筹) pct≤20%", lambda r: pct_rank(r["net_ratio"], ratios) <= EXTREME_PCT_LO),
        ("大流入(抛压) pct≥80%", lambda r: pct_rank(r["net_ratio"], ratios) >= EXTREME_PCT_HI),
        ("大流出 ≤-2%市值", lambda r: r["net_ratio"] <= -0.02),
        ("大流入 ≥+2%市值", lambda r: r["net_ratio"] >= 0.02),
    ]:
        rows = [r for r in panel if sel(r)]
        cells = []
        for h in HORIZONS:
            rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"H{h} {st['mean']*100:+.2f}%/{st['win']*100:.0f}%"
                         if st["n"] >= MIN_N else f"H{h} n={st['n']}")
        print(f"  {name:<22} n={len(rows):>4} | " + " | ".join(cells))


def print_cum_corr(panel: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    for w in CUM_WINDOWS:
        cells = []
        for h in HORIZONS:
            pairs = [(r["cum"][w], r["fwd"][h]) for r in panel
                     if h in r["fwd"] and w in r["cum"] and not math.isnan(r["cum"][w])]
            st = corr_stats([p[0] for p in pairs], [p[1] for p in pairs])
            cells.append(f"H{h}: ρ={st['pearson']:+.2f}" if st["n"] >= MIN_N else f"H{h}:--")
        print(f"  cum{w:<3} | " + " | ".join(cells))


# ── 主流程 ──────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="交易所净流 ↔ 价格相关性投研（面板）")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS, help="净流/价格最少覆盖天数")
    ap.add_argument("--min-med-abs-ret", type=float, default=MIN_MED_ABS_RET,
                    help="稳定币/低波动过滤阈值（日|收益|中位数）")
    ap.add_argument("--out-csv", default="exchange_netflow_price_panel.csv")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        netflow_rows = load_daily_netflow(conn)
        asset_ids = {int(r[0]) for r in netflow_rows}
        # BTC 基准：即使 BTC 不在净流样本，也必须加载其行情做超额收益基准
        with conn.cursor() as cur:
            cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = 'BTC'")
            row = cur.fetchone()
            if row:
                asset_ids.add(int(row[0]))
        market = load_market_daily(conn, asset_ids)
        meta = load_asset_meta(conn, asset_ids)
    print(f"netflow 原始日行={len(netflow_rows)}；行情资产={len(market)}；元数据资产={len(meta)}")

    panel, _mar = build_panel(netflow_rows, market, meta,
                              min_days=args.min_days, min_med_abs_ret=args.min_med_abs_ret)
    if not panel:
        print("无有效面板样本，请调低过滤阈值")
        return

    symbols = sorted({r["symbol"] for r in panel})
    days = sorted({r["date"] for r in panel})
    tot_in = sum(r["netflow_usd"] for r in panel if r["netflow_usd"] > 0)
    tot_out = -sum(r["netflow_usd"] for r in panel if r["netflow_usd"] < 0)
    print(f"\n########## 交易所净流 ↔ 价格 面板（{len(symbols)} 资产 × {len(days)} 日） ##########")
    print(f"  面板行={len(panel)}；{days[0]} ~ {days[-1]}")
    print(f"  累计净流入(转进所)={tot_in/1e6:,.0f}M USD；净流出(转出所)={tot_out/1e6:,.0f}M USD")
    print(f"  样本资产（净流覆盖≥{args.min_days}日，日|收益|中位≥{args.min_med_abs_ret:.3f}）：")
    for s_ in symbols:
        print(f"    {s_}", end=" ")
    print()

    print_panel_corr(panel, "1. 面板相关：日净流 vs 收益")
    print_per_asset_corr(panel, "2. 资产内时序相关（跨资产聚合，净流市值归一）")
    print_bucket_table(panel, "3. 净流市值占比分位 5 桶（可交易口径 T 收盘持有到 T+h）")
    print_extreme_events(panel, "4. 极端净流日事件研究（可交易口径）")
    print_cum_corr(panel, "5. 滚动累计净流市值占比 vs 前瞻收益（Pearson）")
    print_panel_corr(panel, "6. 稳健性：超额收益(减BTC)面板相关", use_excess=True)
    print_per_asset_corr(panel, "7. 稳健性：超额收益(减BTC)资产内相关", use_excess=True)
    print_bucket_table(panel, "8. 稳健性：超额收益(减BTC)分位 5 桶", use_excess=True)

    # 输出面板 CSV
    if args.out_csv:
        path = OUT_DIR / args.out_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["date", "symbol", "netflow_usd", "net_ratio", "net_share",
                      "ret_day"] + [f"fwd{h}" for h in HORIZONS] + \
                     [f"cum{w}" for w in CUM_WINDOWS] + ["btc_ret"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in sorted(panel, key=lambda x: (x["date"], x["symbol"])):
                row = {"date": r["date"].isoformat(), "symbol": r["symbol"],
                       "netflow_usd": round(r["netflow_usd"], 2),
                       "net_ratio": round(r["net_ratio"], 6),
                       "net_share": round(r["net_share"], 4) if not math.isnan(r["net_share"]) else "",
                       "ret_day": round(r["ret_day"], 6), "btc_ret": round(r["btc_day"], 6)}
                for h in HORIZONS:
                    row[f"fwd{h}"] = round(r["fwd"][h], 6) if h in r["fwd"] else ""
                for ww in CUM_WINDOWS:
                    row[f"cum{ww}"] = round(r["cum"][ww], 6) if ww in r["cum"] else ""
                w.writerow(row)
        print(f"\n[CSV] 面板已写: {path}")

    print("\n完成。")


if __name__ == "__main__":
    main()
