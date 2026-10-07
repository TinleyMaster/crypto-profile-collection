#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CVD(主动买卖量) / 多空比 / OI / 基差 / 爆仓 ↔ 代币价格表现 相关性投研（桶粒度）。

容器版：放 scripts/bin/ 由 Zeabur 容器执行（DB 内网访问，避开本地→云数据库带宽瓶颈）。
路径逻辑与 backfill_* 一致（SCRIPT_DIR → ../src），本地与容器均可运行。

数据：
  - biz.cg_taker_volume_hist：CVD 源（cvd_net_ratio=(buy-sell)/(buy+sell)）
  - biz.cg_ls_ratio_hist：多空比（lsr_{top_position/top_account/global_account}）
  - biz.cg_oi_hist：OI（oi_chg_pct = close/open - 1）
  - biz.cg_basis_hist：基差（basis_pct = 桶末基差 %）
  - biz.cg_liq_agg_hist：爆仓不对称（liq_asym = (long-short)/(long+short)）
  - biz.asset_klines：1h K 线（Binance USDT 永续）

口径与纪律：
  - 因子桶 [T, T+iv) → 入场 = 桶末价 close(T+iv)（无前视）→
    fwd_h = close(T+iv+h)/close(T+iv) - 1
  - 每币 ≥ MIN_BUCKETS 有效桶；剔除稳定币/低波动（桶|收益|中位数 < 阈值）
  - 面板 t 仅参考，结论以资产内时序相关聚合为准
  - 稳健性：超额收益（减 BTC 同期）重复相关 + 桶分析

用法：
  python research_cg_factor_corr.py --interval 4h
  python research_cg_factor_corr.py --interval 12h --ratio-type top_position
"""
from __future__ import annotations

import argparse
import bisect
import csv
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

# 各粒度的前瞻小时（h ≥ iv）
HORIZONS = {"4h": (4, 8, 12, 24, 48), "12h": (12, 24, 48, 72, 96)}
MIN_BUCKETS = {"4h": 60, "12h": 30}       # 每币最少有效桶数（4h→10 天，12h→15 天）
MIN_N = 30
MIN_ASSET_N = 10
MIN_MED_ABS_RET = 0.003                   # 稳定币/低波动过滤：桶|收益|中位数下限（0.3%）
OUT_DIR = SCRIPT_DIR.parent.parent / "data"


# ── 数据加载 ──────────────────────────────────────────────────

def load_cvd(conn, interval: str) -> list[tuple]:
    """加载 Coinglass CVD：[(symbol, ts, cvd_net_ratio)]。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, taker_buy_usd, taker_sell_usd
            FROM biz.cg_taker_volume_hist
            WHERE interval = %s AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_lsr(conn, interval: str, ratio_type: str) -> list[tuple]:
    """加载 Coinglass 多空比：[(symbol, ts, ls_ratio)]。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, ls_ratio
            FROM biz.cg_ls_ratio_hist
            WHERE interval = %s AND ratio_type = %s AND ls_ratio IS NOT NULL AND ls_ratio > 0
            ORDER BY symbol, ts
            """, (interval, ratio_type))
        return cur.fetchall()


def load_oi(conn, interval: str) -> list[tuple]:
    """加载 Coinglass OI：[(symbol, ts, oi_open, oi_close)]，因子=桶内 OI 变化率。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, oi_open, oi_close
            FROM biz.cg_oi_hist
            WHERE interval = %s AND oi_open IS NOT NULL AND oi_close IS NOT NULL AND oi_open > 0
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_basis(conn, interval: str) -> list[tuple]:
    """加载 Coinglass 基差：[(symbol, ts, close_basis)]（单位 %，多头溢价）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, close_basis
            FROM biz.cg_basis_hist
            WHERE interval = %s AND close_basis IS NOT NULL
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_liq_agg(conn, interval: str) -> list[tuple]:
    """加载 Coinglass 多所聚合爆仓：[(symbol, ts, long_liq, short_liq)]。

    因子 = 爆仓不对称（long_liq - short_liq）/ (long_liq + short_liq)：
      正=多头爆仓多（价格下跌被强平）/ 负=空头爆仓多（价格上涨被强平）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, long_liq_usd, short_liq_usd
            FROM biz.cg_liq_agg_hist
            WHERE interval = %s AND long_liq_usd IS NOT NULL AND short_liq_usd IS NOT NULL
              AND (long_liq_usd + short_liq_usd) > 0
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_klines_1h(conn, symbols: set[str], lo_ts) -> dict[str, dict]:
    """加载 1h K 线 → {symbol: {hour: close_px}}。"""
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, open_time, close_px
            FROM biz.asset_klines
            WHERE interval = '1h' AND open_time >= %s::timestamp
              AND symbol = ANY(%s::text[])
            ORDER BY symbol, open_time
            """, (lo_ts, list(symbols)))
        for sym, ot, close in cur.fetchall():
            if close is None or float(close) <= 0:
                continue
            out.setdefault(sym, {})[ot] = float(close)
    return out


# ── 统计工具（复用既有口径）────────────────────────────────

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


def corr_stats(xs: list[float], ys: list[float], min_n: int = MIN_N) -> dict:
    n = len(xs)
    if n < min_n:
        return {"n": n}
    pr = _pearson(xs, ys)
    t = pr * math.sqrt(n - 2) / math.sqrt(1 - pr * pr) if abs(pr) < 1 else float("nan")
    return {"n": n, "pearson": pr, "t": t}


def stats_of(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    mean = sum(rets) / len(rets)
    med = statistics.median(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "median": med, "win": win}


def one_sample_t(values: list[float]) -> dict:
    n = len(values)
    if n < 2:
        return {"n": n}
    mean = sum(values) / n
    sd = statistics.stdev(values)
    if sd == 0:
        return {"n": n, "mean": mean, "median": statistics.median(values), "t": float("nan")}
    return {"n": n, "mean": mean, "median": statistics.median(values),
            "pos_share": sum(1 for v in values if v > 0) / n, "t": mean / (sd / math.sqrt(n))}


def excess_ret(row: dict, h: int) -> float:
    base = row["fwd"].get(h)
    btc = row["btc_fwd"].get(h)
    if base is None or btc is None:
        return float("nan")
    return base - btc


# ── 面板构建 ──────────────────────────────────────────────────

def build_panel(factor_rows: list, klines: dict[str, dict], interval: str) -> tuple[list[dict], dict]:
    """构建桶级面板：每行 = 币×桶（含因子、同期/前瞻收益、BTC 基准）。

    入场 = 桶末价（T+iv）；fwd_h 从入场价起算（无前视）。
    factor_rows: [(symbol, ts, factor_value)]
    """
    iv_h = int(interval[:-1]) if interval.endswith("h") else 4
    factor_by_sym: dict[str, dict] = defaultdict(dict)
    for sym, ts, val in factor_rows:
        factor_by_sym[sym][ts] = float(val)

    # BTC 基准（同期桶收益 + 前瞻）
    btc_id = "BTCUSDT"
    btc_px = klines.get(btc_id, {})
    btc_hours = sorted(btc_px)
    bn = len(btc_hours)
    btc_fwd: dict = {}
    btc_day: dict = {}
    for i, d in enumerate(btc_hours):
        btc_fwd[d] = {}
        for h in HORIZONS[interval]:
            if i + h < bn and btc_px[btc_hours[i]] > 0:
                btc_fwd[d][h] = btc_px[btc_hours[i + h]] / btc_px[btc_hours[i]] - 1.0
    for i in range(1, bn):
        if btc_px[btc_hours[i - 1]] > 0:
            btc_day[btc_hours[i]] = btc_px[btc_hours[i]] / btc_px[btc_hours[i - 1]] - 1.0

    panel: list[dict] = []
    per_asset_mar: dict[str, list[float]] = {}

    for sym, fb in factor_by_sym.items():
        px = klines.get(sym)
        if not px:
            continue
        hours = sorted(px)
        idx = {d: i for i, d in enumerate(hours)}
        n = len(hours)

        # 每币桶收益中位数（稳定币过滤）
        rets = {}
        for i in range(1, n):
            if px[hours[i - 1]] > 0:
                rets[hours[i]] = px[hours[i]] / px[hours[i - 1]] - 1.0
        if rets:
            per_asset_mar[sym] = [abs(v) for v in rets.values()]
            if statistics.median([abs(v) for v in rets.values()]) < MIN_MED_ABS_RET:
                continue

        # 桶数过滤
        valid = [ts for ts in fb if ts in idx and idx[ts] + iv_h + max(HORIZONS[interval]) < n]
        if len(valid) < MIN_BUCKETS[interval]:
            continue

        for ts in valid:
            i_entry = idx[ts] + iv_h                # 桶末 = 入场索引
            entry_px = px[hours[i_entry]]
            if entry_px <= 0:
                continue
            fwd: dict = {}
            for h in HORIZONS[interval]:
                fwd[h] = px[hours[i_entry + h]] / entry_px - 1.0
            # 同期桶收益 = [ts+iv, ts] 桶的收益（入场价相对前值）
            i_prev = idx[ts]
            ret_bucket = entry_px / px[hours[i_prev]] - 1.0 if px[hours[i_prev]] > 0 else 0.0
            panel.append({
                "symbol": sym, "bucket": ts, "factor": fb[ts], "ret_bucket": ret_bucket,
                "fwd": fwd, "btc_fwd": btc_fwd.get(hours[i_entry], {}),
                "btc_day": btc_day.get(hours[i_entry], 0.0),
            })
    return panel, per_asset_mar


# ── 输出 ──────────────────────────────────────────────────────

def print_panel_corr(panel: list[dict], title: str, use_excess: bool = False) -> None:
    print(f"\n=== {title} ===")
    xs_all = [r["factor"] for r in panel]
    labels = [f"同期(T~T+{4 if '4h' in title else 12}h)"] + \
             [f"前瞻 T+{h}h" for h in HORIZONS["4h" if "4h" in title else "12h"]]
    for label, h in zip(labels, ["same"] + list(HORIZONS["4h" if "4h" in title else "12h"])):
        if h == "same":
            ys = [r["ret_bucket"] - r["btc_day"] for r in panel] if use_excess else [r["ret_bucket"] for r in panel]
        else:
            ys = [excess_ret(r, h) for r in panel] if use_excess else [r["fwd"][h] for r in panel if h in r["fwd"]]
        pairs = [(x, y) for x, y in zip(xs_all, ys) if y is not None and not math.isnan(y)]
        st = corr_stats([p[0] for p in pairs], [p[1] for p in pairs])
        if st["n"] < MIN_N:
            print(f"  {label:<16} n={st['n']}")
        else:
            print(f"  {label:<16} n={st['n']:<5} ρ={st['pearson']:+.3f} (t={st['t']:+.2f})")


def print_per_asset_corr(panel: list[dict], title: str, use_excess: bool = False) -> None:
    print(f"\n=== {title} ===")
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for r in panel:
        by_asset[r["symbol"]].append(r)
    hs = HORIZONS["4h" if "4h" in title else "12h"]
    labels = [f"同期(T~T+{4 if '4h' in title else 12}h)"] + [f"前瞻 T+{h}h" for h in hs]
    print(f"  {'口径':<16} {'资产数':>5} | 平均ρ | 中位ρ | ρ>0占比 | t")
    for label, h in zip(labels, ["same"] + list(hs)):
        rhos: list[float] = []
        for _sym, rows in by_asset.items():
            pairs = []
            for r in rows:
                if h == "same":
                    y = r["ret_bucket"] - r["btc_day"] if use_excess else r["ret_bucket"]
                else:
                    y = excess_ret(r, h) if use_excess else r["fwd"].get(h)
                if y is not None and not math.isnan(y):
                    pairs.append((r["factor"], y))
            if len(pairs) >= MIN_ASSET_N:
                rhos.append(_pearson([p[0] for p in pairs], [p[1] for p in pairs]))
        rhos = [x for x in rhos if not math.isnan(x)]
        st = one_sample_t(rhos)
        if st["n"] < 5:
            print(f"  {label:<16} {st['n']:>5}  | 样本不足")
        else:
            print(f"  {label:<16} {st['n']:>5}  | {st['mean']:+.3f}  | {st['median']:+.3f}"
                  f"  | {st['pos_share']:>5.0%}   | {st['t']:+.2f}")


def print_bucket_table(panel: list[dict], title: str, use_excess: bool = False) -> None:
    print(f"\n=== {title} ===")
    xs_all = sorted([r["factor"] for r in panel])
    hs = HORIZONS["4h" if "4h" in title else "12h"]
    buckets: dict[str, list[dict]] = {f"Q{i}": [] for i in range(1, 6)}
    for r in panel:
        q = min(5, int(bisect.bisect_right(xs_all, r["factor"]) / len(xs_all) * 5) + 1)
        buckets[f"Q{q}"].append(r)
    baseline = {h: stats_of([excess_ret(r, h) if use_excess else r["fwd"][h] for r in panel
                             if h in r["fwd"] and (not use_excess or not math.isnan(excess_ret(r, h)))])
                for h in hs}
    print(f"  {'桶':<4} {'因子分位':<10} {'n':>5} | " + " | ".join(f"H{h}h" for h in hs))
    for i in range(1, 6):
        rows = buckets[f"Q{i}"]
        lo, hi = (i - 1) / 5, i / 5
        cells = []
        for h in hs:
            if use_excess:
                rets = [excess_ret(r, h) for r in rows if h in r["fwd"] and not math.isnan(excess_ret(r, h))]
            else:
                rets = [r["fwd"][h] for r in rows if h in r["fwd"]]
            st = stats_of(rets)
            cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else f"  --/--")
        print(f"  Q{i}   [{lo:.0%},{hi:.0%})  {len(rows):>5} | " + " | ".join(cells))
    bcells = []
    for h in hs:
        st = baseline[h]
        bcells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%" if st["n"] >= MIN_N else f"  --/--")
    print(f"  基线  [全样本]  {len(panel):>5} | " + " | ".join(bcells))


# ── 主流程 ──────────────────────────────────────────────────

def _run_factor(panel, klines, interval: str, name: str, base_num: int) -> int:
    """对一个因子面板输出全套分析（面板/资产内/分位桶/超额）。返回下一个编号。"""
    syms_n = len({r["symbol"] for r in panel})
    print(f"\n########## {name} ↔ 价格（{interval}，{syms_n} 币） ##########")
    if not panel:
        print(f"{name} 面板为空")
        return base_num
    t_ = f"{name} {interval}"
    print_panel_corr(panel, f"{base_num}. 面板相关：{t_}")
    print_per_asset_corr(panel, f"{base_num + 1}. 资产内时序相关（{t_}）")
    print_bucket_table(panel, f"{base_num + 2}. {t_} 分位 5 桶")
    print_panel_corr(panel, f"{base_num + 3}. 稳健性：超额(减BTC)面板（{t_}）", use_excess=True)
    print_per_asset_corr(panel, f"{base_num + 4}. 稳健性：超额(减BTC)资产内（{t_}）", use_excess=True)
    return base_num + 5


def main() -> None:
    ap = argparse.ArgumentParser(description="CVD/多空比/OI/基差/爆仓 ↔ 价格 桶级相关性投研（Coinglass 历史）")
    ap.add_argument("--interval", choices=("4h", "12h"), default="4h")
    ap.add_argument("--ratio-type", default="top_position",
                    choices=("top_position", "top_account", "global_account"))
    ap.add_argument("--out-csv", default="")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        cvd_rows = load_cvd(conn, args.interval)
        lsr_rows = load_lsr(conn, args.interval, args.ratio_type)
        oi_rows = load_oi(conn, args.interval)
        basis_rows = load_basis(conn, args.interval)
        liq_rows = load_liq_agg(conn, args.interval)
        all_rows = cvd_rows + lsr_rows + oi_rows + basis_rows + liq_rows
        syms = {r[0] for r in all_rows} | {"BTCUSDT"}
        lo = min([r[1] for r in all_rows], default=None)
        if lo is None:
            print("无 Coinglass 历史数据，请先运行 backfill 脚本")
            return
        klines = load_klines_1h(conn, syms, lo)

    print(f"粒度={args.interval} | cvd={len(cvd_rows)} lsr({args.ratio_type})={len(lsr_rows)} "
          f"oi={len(oi_rows)} basis={len(basis_rows)} liq_agg={len(liq_rows)} | K线币数={len(klines)}")

    num = 1
    # CVD 净主动占比
    cvd_panel, _ = build_panel([(s, t, (float(b) - float(sl)) / (float(b) + float(sl)))
                                for s, t, b, sl in cvd_rows if (float(b) + float(sl)) > 0],
                               klines, args.interval)
    num = _run_factor(cvd_panel, klines, args.interval, "cvd_net_ratio", num)
    # 多空比
    lsr_panel, _ = build_panel(lsr_rows, klines, args.interval)
    num = _run_factor(lsr_panel, klines, args.interval, f"lsr_{args.ratio_type}", num)
    # OI 变化率（桶内 oi_close/oi_open - 1）
    oi_panel, _ = build_panel([(s, t, float(c) / float(o) - 1.0)
                               for s, t, o, c in oi_rows if float(o) > 0],
                              klines, args.interval)
    num = _run_factor(oi_panel, klines, args.interval, "oi_chg_pct", num)
    # 基差（多头溢价 %）
    basis_panel, _ = build_panel(basis_rows, klines, args.interval)
    num = _run_factor(basis_panel, klines, args.interval, "basis_pct", num)
    # 爆仓不对称 (long-short)/(long+short)
    liq_panel, _ = build_panel([(s, t, (float(lg) - float(sh)) / (float(lg) + float(sh)))
                                for s, t, lg, sh in liq_rows if (float(lg) + float(sh)) > 0],
                               klines, args.interval)
    num = _run_factor(liq_panel, klines, args.interval, "liq_asym", num)

    # CSV
    if args.out_csv:
        path = OUT_DIR / args.out_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["bucket", "symbol", "factor", "ret_bucket"] + [f"fwd{h}" for h in HORIZONS[args.interval]]
        rows = cvd_panel + lsr_panel + oi_panel + basis_panel + liq_panel
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in sorted(rows, key=lambda x: (x["bucket"], x["symbol"])):
                row = {"bucket": r["bucket"].isoformat(), "symbol": r["symbol"],
                       "factor": round(r["factor"], 6), "ret_bucket": round(r["ret_bucket"], 6)}
                for h in HORIZONS[args.interval]:
                    row[f"fwd{h}"] = round(r["fwd"][h], 6) if h in r["fwd"] else ""
                w.writerow(row)
        print(f"\n[CSV] 面板已写: {path}")

    print("\n完成。")


if __name__ == "__main__":
    main()
