#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DefiLlama TVL 变化 ↔ 代币价格表现 相关性投研（面板，只读，2026-10-04）。

数据：
  - biz.protocol_metric_daily：资产×日 TVL（source_code='dl'，2026-08-28 ~ 2026-10-03）
      注意：库内 tvl_change_1d / tvl_change_7d 字段严重污染（DefiLlama 源字段语义混乱、
      含 1e22 级脏值），**一律弃用**，变化率由 TVL 序列自算。
  - biz.asset_market_daily：资产日行情（Python 端按 cmc > cmc_historical > binance_klines 去重）
  - core.asset：symbol / canonical_name / market_cap_rank

口径与纪律：
  - tvl_ret(T)   = tvl(T) / tvl(T-1) - 1（同一资产相邻日，自算）
  - tvl_ret_adj(T)= tvl_ret(T) - 同期自身币价收益（近似剥离「币价成分」→ 锁定量/资金流变化，
                   因 DefiLlama TVL 以 USD 计价，TVL 变化天然含币价涨跌，内生性需处理）
  - tvl_ret_7d(T)= tvl(T) / tvl(T-7) - 1（7 日累计，慢变量）
  - 面板相关中 tvl_ret 裁剪到 ±100%，避免迁移/上币等一次性跳变主导
  - 日收益 = market_daily price_usd 环比（剔除 is_anomaly 行）
  - 样本过滤：TVL 覆盖 ≥ 10 日；价格覆盖 ≥ 10 日；剔除稳定币/低波动资产
    （日|收益|中位数 < 0.3%）；剔除 market_cap_rank 缺失资产
  - 前瞻收益：fwd_h = price(T+h)/price(T) - 1，h=1/2/3/5，无前视
  - 超额收益：asset_ret - BTC_ret（同期，控制市场 beta）作为稳健性对照

分析维度：
  1. 面板相关：tvl_ret / tvl_ret_adj vs 同期 + 前瞻 1/2/3/5 日收益
     （Pearson + Spearman + 近似 t；面板 t 仅作参考，结论以资产内相关为准）
  2. 资产内时间序列相关：每资产 Pearson(tvl_ret, ret_{T+k})，跨资产聚合均值/中位/正负占比 + t
  3. 分位 5 桶：tvl_ret 全面板分位（Q1=TVL 大缩水 → Q5=TVL 大增长）前瞻收益均值/胜率 vs 基线
  4. 极端事件：tvl_ret ≥ ±10%、±30%（TVL 大幅变动日）事件研究（H1/H3/H5）
  5. 累计 TVL 变化（7 日）vs 前瞻收益（慢变量检验）
  6. 稳健性：超额收益（vs BTC）重复相关 + 桶分析
  7. 输出对齐面板 CSV（data/tvl_price_panel.csv）

结论解读（重要）：
  若「TVL 增长 = 需求增长」假设成立，tvl_ret 与前瞻收益应**正相关**；
  但同期正相关高度可能是 TVL 计价含币价的内生性，需看 tvl_ret_adj 与超额收益口径。
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
EXTREME_PCT_HI = 0.80
EXTREME_PCT_LO = 0.20
MIN_N = 30          # 统计量最小样本（面板口径）
MIN_ASSET_N = 6     # 资产内相关聚合时，单资产最少配对日
MIN_DAYS = 10       # 单资产最少 TVL/价格覆盖天数（窗口仅 34 天，取低阈值）
MIN_MED_ABS_RET = 0.003  # 稳定币/低波动过滤：日|收益|中位数下限
CLIP = 1.0          # tvl_ret 裁剪幅度（±100%）
MAX_ABS_RET = 3.0   # 收益异常过滤：|ret| 超过 ±300% 的行剔除（价格跳变/上币/归零，避免污染均值）
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
WINDOW_START = "2026-08-01"


# ── 数据加载 ──────────────────────────────────────────────────

def load_daily_tvl(conn) -> list[tuple]:
    """加载资产×日 TVL：(asset_id, date, tvl)。利用 metric_date 索引，只查窗口内。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, metric_date, tvl
            FROM biz.protocol_metric_daily
            WHERE metric_date >= %s::date
              AND tvl IS NOT NULL AND tvl > 0
            ORDER BY asset_id, metric_date
            """, (WINDOW_START,))
        return cur.fetchall()


def load_market_daily(conn, asset_ids: set[int], chunk: int = 400) -> dict[int, dict]:
    """加载资产×日行情 → {asset_id: {date: (price, market_cap)}}。

    直查基表，Python 端按源优先级去重：cmc > cmc_historical > binance_klines。
    asset_id 列表分块（ANY 数组过大会致远端连接中断）。
    """
    src_prio = {"cmc": 0, "cmc_historical": 1, "binance_klines": 2}
    best: dict[int, dict] = defaultdict(dict)
    ids = sorted(asset_ids)
    with conn.cursor() as cur:
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            cur.execute(
                """
                SELECT asset_id, market_date, source_code, price_usd, market_cap
                FROM biz.asset_market_daily
                WHERE market_date >= %s::date
                  AND asset_id = ANY(%s::int[])
                  AND price_usd IS NOT NULL AND price_usd > 0
                  AND (is_anomaly IS NOT TRUE OR is_anomaly IS NULL)
                ORDER BY asset_id, market_date
                """, (WINDOW_START, part))
            for aid, d, src, price, mc in cur.fetchall():
                prio = src_prio.get(src, 9)
                cur_best = best[aid].get(d)
                if cur_best is None or prio < cur_best[0]:
                    best[aid][d] = (prio, float(price), float(mc) if mc else None)
    return {aid: {d: (v[1], v[2]) for d, v in days.items()} for aid, days in best.items()}


def load_asset_meta(conn, asset_ids: set[int]) -> dict[int, dict]:
    """加载资产元数据：symbol / name / market_cap_rank（仅 TVL 资产）。"""
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

def build_panel(tvl_rows: list, market: dict[int, dict], meta: dict[int, dict],
                min_days: int = MIN_DAYS, min_med_abs_ret: float = MIN_MED_ABS_RET) -> list[dict]:
    """构建面板：每行 = 资产×日（含 TVL 变化率、剥离币价后的变化率、同期/前瞻收益）。"""
    tvl_by_asset: dict[int, dict] = defaultdict(dict)   # asset_id -> {date: tvl}
    for aid, d, tvl in tvl_rows:
        tvl_by_asset[aid][d] = float(tvl)

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

    for aid, tvls in tvl_by_asset.items():
        meta_a = meta.get(aid)
        if not meta_a or meta_a["rank"] is None:
            continue
        if len(tvls) < min_days:
            continue
        px = market.get(aid)
        if not px or len(px) < min_days:
            continue

        tdates = sorted(tvls)
        t_idx = {d: i for i, d in enumerate(tdates)}
        # TVL 日变化率（同资产相邻日自算）
        tvl_ret: dict = {}
        for i in range(1, len(tdates)):
            p0, p1 = tvls[tdates[i - 1]], tvls[tdates[i]]
            if p0 > 0:
                tvl_ret[tdates[i]] = p1 / p0 - 1.0
        if len(tvl_ret) < min_days - 1:
            continue

        # 日收益序列（连续日）
        dates = sorted(px)
        p_idx = {d: i for i, d in enumerate(dates)}
        rets = {}
        for i in range(1, len(dates)):
            rets[dates[i]] = px[dates[i]][0] / px[dates[i - 1]][0] - 1.0
        if not rets:
            continue
        # 稳定币/低波动过滤
        med_abs = statistics.median([abs(v) for v in rets.values() if v is not None])
        if med_abs < min_med_abs_ret:
            continue

        n = len(dates)
        for d, tret in tvl_ret.items():
            if d not in px or d not in p_idx:
                continue
            i = p_idx[d]
            if i + 1 >= n:
                continue
            ret_day = px[dates[i]][0] / px[dates[i - 1]][0] - 1.0 if i >= 1 else 0.0
            close_t = px[d][0]
            if close_t <= 0:
                continue
            fwd: dict[int, float] = {}
            for h in HORIZONS:
                if i + h < n:
                    fwd[h] = px[dates[i + h]][0] / close_t - 1.0
            # 收益异常过滤：同期或任一前瞻收益 |x| > MAX_ABS_RET 的行剔除
            if abs(ret_day) > MAX_ABS_RET:
                continue
            if any(h in fwd and abs(fwd[h]) > MAX_ABS_RET for h in HORIZONS):
                continue
            mc = px[d][1]
            if not mc or mc <= 0:
                continue
            # 剥离币价成分的 TVL 变化（近似锁定量/资金流变化）
            tret_adj = tret - ret_day
            # 7 日累计 TVL 变化（含当日，回看至多 7 个 TVL 日）
            tret_7d = float("nan")
            j = t_idx[d]
            if t_idx.get(d) is not None:
                cnt = 0
                k = j
                while k >= 0 and cnt < 7:
                    cnt += 1
                    k -= 1
                if k >= 0 and tvls[tdates[k]] > 0:
                    tret_7d = tvls[d] / tvls[tdates[k]] - 1.0
            panel.append({
                "asset_id": aid, "symbol": meta_a["symbol"], "name": meta_a["name"],
                "date": d, "tvl": tvls[d], "tvl_ret": max(-CLIP, min(CLIP, tret)),
                "tvl_ret_raw": tret, "tvl_ret_adj": tret_adj, "tvl_ret_7d": tret_7d,
                "ret_day": ret_day, "fwd": fwd,
                "btc_fwd": btc_fwd.get(d, {}), "btc_day": btc_day.get(d, 0.0),
            })

    return panel


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
    x_plain = [r["tvl_ret"] for r in panel]
    x_adj = [r["tvl_ret_adj"] for r in panel]
    labels = ["同期(T日)", "前瞻 T→T+1", "前瞻 T→T+2", "前瞻 T→T+3", "前瞻 T→T+5"]
    print(f"  {'口径':<12} | 原始TVL变化 ρ (t) | 剥离币价TVL变化 ρ (t) | spearman(剥离)")
    for label, h in zip(labels, ["same", 1, 2, 3, 5]):
        if use_excess:
            ys = [excess_ret(r, h) for r in panel] if h != "same" else \
                 [r["ret_day"] - r["btc_day"] for r in panel]
        else:
            ys = [r["fwd"][h] for r in panel if h in r["fwd"]] if h != "same" else \
                 [r["ret_day"] for r in panel]
        pairs = [(x, y) for x, y in zip(x_plain, ys) if y is not None and not math.isnan(y)]
        st_raw = corr_stats([p[0] for p in pairs], [p[1] for p in pairs])
        if st_raw["n"] < MIN_N:
            print(f"  {label:<12} n={st_raw['n']}")
            continue
        if h == "same":
            # 剥离口径同期 = corr(tvl_ret - ret_day, ret_day) 是构造性伪相关，不输出
            print(f"  {label:<12} {st_raw['pearson']:+.3f} (t={st_raw['t']:+.2f})"
                  f"     | 构造性伪相关(n/a)     | n/a")
            continue
        pairs2 = [(x, y) for x, y in zip(x_adj, ys) if y is not None and not math.isnan(y) and not math.isnan(x)]
        st_adj = corr_stats([p[0] for p in pairs2], [p[1] for p in pairs2])
        print(f"  {label:<12} {st_raw['pearson']:+.3f} (t={st_raw['t']:+.2f})"
              f"     | {st_adj['pearson']:+.3f} (t={st_adj['t']:+.2f})"
              f"     | {st_adj['spearman']:+.3f}")


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
                if y is not None and not math.isnan(y) and not math.isnan(r["tvl_ret"]):
                    pairs.append((r["tvl_ret"], y))
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
    ratios = [r["tvl_ret"] for r in panel]
    buckets: dict[str, list[dict]] = {f"Q{i}": [] for i in range(1, 6)}
    for r in panel:
        q = min(5, int(pct_rank(r["tvl_ret"], ratios) * 5) + 1)
        buckets[f"Q{q}"].append(r)
    all_rets = {h: [r["fwd"][h] for r in panel if h in r["fwd"]] for h in HORIZONS}
    if use_excess:
        all_rets = {h: [excess_ret(r, h) for r in panel
                        if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
                    for h in HORIZONS}
    baseline = {h: stats_of(all_rets[h]) for h in HORIZONS}
    print(f"  {'桶':<4} {'TVL变化区间':<12} {'n':>5} | " + " | ".join(f"H{h}均/胜" for h in HORIZONS))
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
        med = statistics.median([r["tvl_ret"] for r in rows]) if rows else float("nan")
        print(f"  Q{i}   [{lo:.0%},{hi:.0%})  {len(rows):>5} | " + " | ".join(cells)
              + f"    (中位TVL日变{med*100:+.1f}%)")
    bcells = []
    for h in HORIZONS:
        st = baseline[h]
        bcells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else f"  --/--")
    print(f"  基线  [全样本]  {len(panel):>5} | " + " | ".join(bcells))


def print_extreme_events(panel: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    for name, thr in [
        ("TVL日增 ≥+10%", 0.10), ("TVL日减 ≤-10%", -0.10),
        ("TVL日增 ≥+30%", 0.30), ("TVL日减 ≤-30%", -0.30),
    ]:
        rows = [r for r in panel if r["tvl_ret_raw"] >= thr] if thr > 0 else \
               [r for r in panel if r["tvl_ret_raw"] <= thr]
        cells = []
        for h in HORIZONS:
            rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"H{h} {st['mean']*100:+.2f}%/{st['win']*100:.0f}%"
                         if st["n"] >= MIN_N else f"H{h} n={st['n']}")
        print(f"  {name:<16} n={len(rows):>4} | " + " | ".join(cells))


def print_cum_corr(panel: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    cells = []
    for h in HORIZONS:
        pairs = [(r["tvl_ret_7d"], r["fwd"][h]) for r in panel
                 if h in r["fwd"] and not math.isnan(r["tvl_ret_7d"])]
        st = corr_stats([p[0] for p in pairs], [p[1] for p in pairs])
        cells.append(f"H{h}: ρ={st['pearson']:+.2f} (t={st['t']:+.2f})" if st["n"] >= MIN_N else f"H{h}:--")
    print(f"  tvl_ret_7d（7日累计TVL变化）| " + " | ".join(cells))


# ── 主流程 ──────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="TVL 变化 ↔ 价格相关性投研（面板）")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS, help="TVL/价格最少覆盖天数")
    ap.add_argument("--min-med-abs-ret", type=float, default=MIN_MED_ABS_RET,
                    help="稳定币/低波动过滤阈值（日|收益|中位数）")
    ap.add_argument("--out-csv", default="tvl_price_panel.csv")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        tvl_rows = load_daily_tvl(conn)
        asset_ids = {int(r[0]) for r in tvl_rows}
        # BTC 基准：即使 BTC 不在 TVL 样本，也必须加载其行情做超额收益基准
        with conn.cursor() as cur:
            cur.execute("SELECT asset_id FROM core.asset WHERE canonical_symbol = 'BTC'")
            row = cur.fetchone()
            if row:
                asset_ids.add(int(row[0]))
        market = load_market_daily(conn, asset_ids)
        meta = load_asset_meta(conn, asset_ids)
    print(f"tvl 原始日行={len(tvl_rows)}；行情资产={len(market)}；元数据资产={len(meta)}")

    panel = build_panel(tvl_rows, market, meta,
                        min_days=args.min_days, min_med_abs_ret=args.min_med_abs_ret)
    if not panel:
        print("无有效面板样本，请调低过滤阈值")
        return

    symbols = sorted({r["symbol"] for r in panel})
    days = sorted({r["date"] for r in panel})
    print(f"\n########## TVL 变化 ↔ 价格 面板（{len(symbols)} 资产 × {len(days)} 日） ##########")
    print(f"  面板行={len(panel)}；{days[0]} ~ {days[-1]}")
    print(f"  累计TVL合计={sum(r['tvl'] for r in panel)/1e9:,.0f}B USD；样本资产：")
    for s_ in symbols:
        print(f"    {s_}", end=" ")
    print()

    print_panel_corr(panel, "1. 面板相关：TVL 日变化率 vs 收益")
    print_per_asset_corr(panel, "2. 资产内时序相关（跨资产聚合，结论基准）")
    print_bucket_table(panel, "3. TVL 日变化率分位 5 桶（可交易口径 T 收盘持有到 T+h）")
    print_extreme_events(panel, "4. 极端 TVL 变动日事件研究（可交易口径）")
    print_cum_corr(panel, "5. 7 日累计 TVL 变化 vs 前瞻收益（慢变量检验）")
    print_panel_corr(panel, "6. 稳健性：超额收益(减BTC)面板相关", use_excess=True)
    print_per_asset_corr(panel, "7. 稳健性：超额收益(减BTC)资产内相关", use_excess=True)
    print_bucket_table(panel, "8. 稳健性：超额收益(减BTC)分位 5 桶", use_excess=True)

    # 输出面板 CSV
    if args.out_csv:
        path = OUT_DIR / args.out_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["date", "symbol", "tvl", "tvl_ret", "tvl_ret_adj", "tvl_ret_7d",
                      "ret_day"] + [f"fwd{h}" for h in HORIZONS] + ["btc_ret"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in sorted(panel, key=lambda x: (x["date"], x["symbol"])):
                row = {"date": r["date"].isoformat(), "symbol": r["symbol"],
                       "tvl": round(r["tvl"], 2),
                       "tvl_ret": round(r["tvl_ret"], 6),
                       "tvl_ret_adj": round(r["tvl_ret_adj"], 6),
                       "tvl_ret_7d": round(r["tvl_ret_7d"], 6) if not math.isnan(r["tvl_ret_7d"]) else "",
                       "ret_day": round(r["ret_day"], 6), "btc_ret": round(r["btc_day"], 6)}
                for h in HORIZONS:
                    row[f"fwd{h}"] = round(r["fwd"][h], 6) if h in r["fwd"] else ""
                w.writerow(row)
        print(f"\n[CSV] 面板已写: {path}")

    print("\n完成。")


if __name__ == "__main__":
    main()
