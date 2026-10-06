#!/usr/bin/env python3
"""细粒度涨幅分档 × 交易参数回测（2023~2026，529 合约 1h）。

对每个涨幅档位（5~10%, 10~20%, 20~30%, 30~50%, 50~75%, 75~100%, 100%+），
分别测试做多和做空方向，输出最优参数组合（止盈/止损/持仓时间/杠杆）。

复用 backtest_trade_params.py 的事件缓存（trade_params_events.csv）。

用法：python bin/backtest_bucket_params.py [--cost 0.002]
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

DATA_DIR = SCRIPT_DIR.parent / "data"
CACHE = DATA_DIR / "trade_params_events.csv"

# 参数网格
N_LIST = [6, 12, 24, 36, 48, 72, 96, 120, 144, 168]
TP_LIST = [0.0, 0.05, 0.10, 0.20, 0.30, 0.50]
SL_LIST = [0.0, 0.03, 0.05, 0.08, 0.10]
TR_LIST = [0.03, 0.05, 0.08, 0.10, 0.15]
LEV_LIST = [1, 3, 5]
MIN_N = 30
WORST_LIMIT = -0.15

# 涨幅分档（双向测试）
BUCKETS = [
    ("5~10%",   0.05, 0.10),
    ("10~20%",  0.10, 0.20),
    ("20~30%",  0.20, 0.30),
    ("30~50%",  0.30, 0.50),
    ("50~75%",  0.50, 0.75),
    ("75~100%", 0.75, 1.00),
    ("100%+",   1.00, 9.99),
]


def load_events() -> list[dict]:
    if not CACHE.exists():
        print(f"[ERROR] 缓存不存在: {CACHE}")
        print("请先运行 python bin/backtest_trade_params.py 生成缓存")
        sys.exit(1)
    with CACHE.open(newline="") as f:
        rd = csv.DictReader(f)
        rows = [dict(r) for r in rd]
    for r in rows:
        r["eo"] = datetime.fromisoformat(r["eo"])
        for k in list(rows[0].keys()):
            if k in ("symbol", "eo"):
                continue
            v = r[k]
            r[k] = None if v in (None, "") else float(v)
    return rows


def _vec_rows(rows):
    TR_PH_COLS = ["ph_tr3", "ph_tr5", "ph_tr8", "ph_tr10", "ph_tr15"]
    HP_COLS = [("hp2", 0.02), ("hp3", 0.03), ("hp5", 0.05), ("hp8", 0.08),
               ("hp10", 0.10), ("hp20", 0.20), ("hp30", 0.30), ("hp50", 0.50)]
    SL_COLS = [("sl2", 0.02), ("sl3", 0.03), ("sl5", 0.05), ("sl8", 0.08), ("sl10", 0.10),
               ("sl20", 0.20), ("sl30", 0.30), ("sl50", 0.50)]
    C_COLS = [(f"c{n}", n) for n in N_LIST]
    TR_COLS = [("tr3", 0.03), ("tr5", 0.05), ("tr8", 0.08), ("tr10", 0.10), ("tr15", 0.15)]
    keys = (["entry", "chg24", "eo", "px_peak"] + [c for c, _ in HP_COLS]
            + [c for c, _ in SL_COLS] + [c for c, _ in C_COLS] + [c for c, _ in TR_COLS]
            + TR_PH_COLS)
    out: dict[str, np.ndarray] = {}
    for k in keys:
        if k == "eo":
            out[k] = np.asarray([r[k].timestamp() for r in rows], dtype=float)
        else:
            out[k] = np.asarray([np.nan if r.get(k) is None else float(r[k]) for r in rows],
                                dtype=float)
    return out


def _returns_vec(arr, direction, n, tp, sl, tr, cost=0.0):
    """向量化单笔净收益（含成本）。"""
    entry = arr["entry"]
    cn = arr[f"c{n}"]
    valid = ~np.isnan(cn)
    if direction == 1:
        if tr is not None:
            t_tp = arr[f"tr{int(tr*100)}"]
        else:
            t_tp = np.full_like(entry, np.nan) if tp == 0 else arr[f"hp{int(tp*100)}"]
        t_sl = np.full_like(entry, np.nan) if sl == 0 else arr[f"sl{int(sl*100)}"]
    else:
        t_tp = np.full_like(entry, np.nan) if tp == 0 else arr[f"sl{int(tp*100)}"]
        t_sl = np.full_like(entry, np.nan) if sl == 0 else arr[f"hp{int(sl*100)}"]
    t_tp = np.where(t_tp <= n, t_tp, np.nan)
    t_sl = np.where(t_sl <= n, t_sl, np.nan)
    ret_time = direction * (cn / entry - 1)
    hit_tp = ~np.isnan(t_tp) & (np.isnan(t_sl) | (t_tp < t_sl))
    hit_sl = ~np.isnan(t_sl) & (np.isnan(t_tp) | (t_sl <= t_tp))
    if tr is not None:
        ph_at_tr = arr[f"ph_tr{int(tr * 100)}"]
        tp_val = ph_at_tr * (1 - tr) / entry - 1
    else:
        tp_val = tp
    ret = np.where(hit_tp, tp_val, np.where(hit_sl, -sl, ret_time))
    ret = np.where(valid, ret, np.nan)
    # 杠杆收益（爆仓 = -100%）
    # 净收益 = 毛收益 - 成本
    ret = ret - cost
    return ret


def _apply_lev(ret, lev):
    """杠杆收益：ret_lev = ret * lev，但不低于 -1（爆仓）。"""
    return np.maximum(ret * lev, -1.0)


def grid_bucket(arr, direction, cost=0.0, lev_list=None):
    """单个桶×方向的网格搜索。返回 (fix_best, trail_best)。"""
    if lev_list is None:
        lev_list = LEV_LIST
    fix_results = []
    trail_results = []

    for n in N_LIST:
        cn = arr[f"c{n}"]
        if np.isnan(cn).sum() > 0 and len(cn) - np.isnan(cn).sum() < MIN_N:
            continue
        for sl in SL_LIST:
            # FIX 模式
            for tp in TP_LIST:
                ret = _returns_vec(arr, direction, n, tp, sl, None, cost)
                for lev in lev_list:
                    r = _apply_lev(ret, lev) if lev > 1 else ret
                    rs = r[~np.isnan(r)]
                    if len(rs) < MIN_N:
                        continue
                    fix_results.append({
                        "mode": "FIX", "N": n, "TP": tp, "TR": None, "SL": sl, "lev": lev,
                        "n": len(rs), "win": float((rs > 0).mean()),
                        "mean": float(rs.mean()), "med": float(np.median(rs)),
                        "pf": float((rs[rs > 0].sum() + 1e-12) / (abs(rs[rs <= 0].sum()) + 1e-12)),
                        "worst": float(np.nanmin(rs)),
                    })
            # TRAIL 模式（仅做多）
            if direction == 1:
                for tr in TR_LIST:
                    ret = _returns_vec(arr, direction, n, 0, sl, tr, cost)
                    for lev in lev_list:
                        r = _apply_lev(ret, lev) if lev > 1 else ret
                        rs = r[~np.isnan(r)]
                        if len(rs) < MIN_N:
                            continue
                        trail_results.append({
                            "mode": "TRAIL", "N": n, "TP": None, "TR": tr, "SL": sl, "lev": lev,
                            "n": len(rs), "win": float((rs > 0).mean()),
                            "mean": float(rs.mean()), "med": float(np.median(rs)),
                            "pf": float((rs[rs > 0].sum() + 1e-12) / (abs(rs[rs <= 0].sum()) + 1e-12)),
                            "worst": float(np.nanmin(rs)),
                        })

    def _best(results):
        safe = [g for g in results if g["worst"] >= WORST_LIMIT]
        pool = safe if len(safe) >= 3 else results
        pool.sort(key=lambda g: (g["pf"], g["win"]), reverse=True)
        return pool[:5] if pool else []

    return _best(fix_results), _best(trail_results)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.002, help="往返成本（默认 0.2%）")
    ap.add_argument("--leverage", nargs="+", type=int, default=LEV_LIST)
    args = ap.parse_args()

    rows = load_events()
    print(f"[events] {len(rows):,} 条；成本 {args.cost*100:.1f}%")

    out_csv = DATA_DIR / "backtest_bucket_params.csv"
    all_rows = []

    for bucket_name, lo, hi in BUCKETS:
        sub = [r for r in rows if lo <= r["chg24"] < hi]
        if len(sub) < MIN_N:
            print(f"\n== {bucket_name} == 样本不足 ({len(sub)})")
            continue
        arr = _vec_rows(sub)

        for direction, dir_label in [(1, "LONG"), (-1, "SHORT")]:
            fix_top, trail_top = grid_bucket(arr, direction, args.cost, args.leverage)

            print(f"\n{'='*78}")
            print(f"== {bucket_name}  方向={dir_label}  事件数={len(sub)} ==")

            if direction == 1 and trail_top:
                print(f"\n  --- TRAIL 模式 TOP5 ---")
                print(f"  {'N(h)':>5} {'TR%':>5} {'SL%':>5} {'Lev':>4} | "
                      f"{'n':>6} {'胜%':>6} {'期望%':>8} {'中位%':>7} {'PF':>6} {'最差%':>7}")
                for g in trail_top[:3]:
                    print(f"  {g['N']:>5} {g['TR']*100:>5.0f} {g['SL']*100:>5.0f} {g['lev']:>4}x | "
                          f"{g['n']:>6,} {g['win']*100:>6.1f} {g['mean']*100:>8.2f} "
                          f"{g['med']*100:>7.2f} {g['pf']:>6.2f} {g['worst']*100:>7.1f}")
                    all_rows.append({"bucket": bucket_name, "dir": dir_label, **g})

            if fix_top:
                print(f"\n  --- FIX 模式 TOP5 ---")
                print(f"  {'N(h)':>5} {'TP%':>5} {'SL%':>5} {'Lev':>4} | "
                      f"{'n':>6} {'胜%':>6} {'期望%':>8} {'中位%':>7} {'PF':>6} {'最差%':>7}")
                for g in fix_top[:3]:
                    print(f"  {g['N']:>5} {g['TP']*100:>5.0f} {g['SL']*100:>5.0f} {g['lev']:>4}x | "
                          f"{g['n']:>6,} {g['win']*100:>6.1f} {g['mean']*100:>8.2f} "
                          f"{g['med']*100:>7.2f} {g['pf']:>6.2f} {g['worst']*100:>7.1f}")
                    all_rows.append({"bucket": bucket_name, "dir": dir_label, **g})

    # 写 CSV
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "dir", "mode", "N_h", "TP_pct", "TR_pct", "SL_pct", "lev",
                     "n", "win", "mean", "med", "pf", "worst_pct"])
        for r in all_rows:
            w.writerow([r["bucket"], r["dir"], r["mode"], r["N"],
                         "" if r["TP"] is None else r["TP"] * 100,
                         "" if r["TR"] is None else r["TR"] * 100,
                         r["SL"] * 100, r["lev"], r["n"],
                         round(r["win"], 4), round(r["mean"], 4),
                         round(r["med"], 4), round(r["pf"], 4), round(r["worst"], 4)])

    # 汇总表
    print(f"\n{'='*78}")
    print("== 各档位最优参数汇总（PF 降序，风险约束 ≥ -15%）==")
    print(f"{'档位':>10} {'方向':>6} {'模式':>6} {'N(h)':>5} {'TP%':>5} {'TR%':>5} {'SL%':>5} {'Lev':>4} | "
          f"{'n':>6} {'胜%':>6} {'期望%':>8} {'PF':>6} {'最差%':>7}")
    print("-" * 100)
    for bucket_name, lo, hi in BUCKETS:
        for dir_label in ["LONG", "SHORT"]:
            best = [r for r in all_rows if r["bucket"] == bucket_name and r["dir"] == dir_label]
            if not best:
                continue
            best.sort(key=lambda g: (g["pf"], g["win"]), reverse=True)
            g = best[0]
            tp_s = f"{g['TP']*100:.0f}" if g["TP"] is not None else "-"
            tr_s = f"{g['TR']*100:.0f}" if g["TR"] is not None else "-"
            print(f"{bucket_name:>10} {dir_label:>6} {g['mode']:>6} {g['N']:>5} {tp_s:>5} {tr_s:>5} "
                  f"{g['SL']*100:>5.0f} {g['lev']:>4}x | "
                  f"{g['n']:>6,} {g['win']*100:>6.1f} {g['mean']*100:>8.2f} "
                  f"{g['pf']:>6.2f} {g['worst']*100:>7.1f}")

    print(f"\n结果已存 {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())