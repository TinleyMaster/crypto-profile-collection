#!/usr/bin/env python3
"""A 信号门槛敏感性：chg24 阈值从 50% 下调，看频率提升 vs 期望衰减的平衡点。

口径（2026-10-07）：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv（2023~2026 全池 529 合约）
  - 出场：TRAIL 3% 跟踪止盈（ph_tr3×0.97/entry-1），成本 0.3%
  - 频率：回测全池月度笔数（2026-01~2026-09 月均，近似实盘信号源上限）
    ⚠️ 实盘受在线合约数(~319)/min_vol/冷却/不叠仓限制，实际约为该值 1/3~1/2

用法：python bin/backtest_a_threshold.py
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

CACHE = SCRIPT_DIR.parent / "data" / "trade_params_events.csv"
TR = 0.03
COST = 0.003
THRESHOLDS = [0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20]


def _stats(ret) -> dict:
    rs = ret[~np.isnan(ret)]
    win = rs > 0
    gw = float(rs[win].sum())
    gl = abs(float(rs[~win].sum())) if (~win).any() else 0.0
    n_w, n_l = int(win.sum()), int((~win).sum())
    payoff = (rs[win].mean() / abs(rs[~win].mean())) if n_l and n_w else float("inf")
    return {
        "n": len(rs), "win": float((rs > 0).mean()), "mean": float(rs.mean()),
        "pf": gw / gl if gl > 0 else float("inf"), "payoff": payoff,
        "worst": float(np.min(rs)),
    }


def main() -> int:
    evs = []
    with CACHE.open("r", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            evs.append(r)

    chg = np.array([float(e["chg24"]) for e in evs])
    ret = np.array([
        float(e["ph_tr3"]) * (1 - TR) / float(e["entry"]) - 1 if e["ph_tr3"] else np.nan
        for e in evs
    ])
    months = np.array([datetime.fromisoformat(e["eo"]).strftime("%Y-%m") for e in evs])

    # 2026-01~09 月均频率（当前市场环境代表）
    mcount = Counter(months)
    recent_months = [m for m in mcount if m >= "2026-01"]
    n_recent_months = max(len(recent_months), 1)

    print("\n" + "=" * 108)
    print("A 信号门槛敏感性（TRAIL3 · 含0.3%成本 · 全池回测）")
    print("=" * 108)
    print(f"  {'门槛chg24':>10}{'事件数':>9}{'相对x':>6}{'胜率%':>8}{'盈亏比':>7}"
          f"{'毛期望%':>9}{'净期望%':>9}{'PF':>7}{'最差%':>8}{'2026月均笔':>10}")
    print("  " + "-" * 104)

    base = None
    rows = []
    for th in THRESHOLDS:
        m = chg >= th
        r = ret[m]
        rn = r - COST
        st = _stats(ret[m])
        n_recent = sum(1 for i, e in enumerate(evs) if m[i]
                       and datetime.fromisoformat(e["eo"]) >= datetime(2026, 1, 1))
        freq = n_recent / n_recent_months
        mult = f"{st['n'] / base:>5.1f}" if base else "1.0"
        if base is None:
            base = st["n"]
        gw = float(rn[rn > 0].sum())
        gl = abs(float(rn[rn <= 0].sum())) if (rn <= 0).any() else 0.0
        pf_n = gw / gl if gl > 0 else float("inf")
        print(f"  {f'≥{th*100:.0f}%':>10}{st['n']:>9,}{mult:>6}{st['win']*100:>8.1f}"
              f"{st['payoff']:>7.2f}{st['mean']*100:>9.2f}{rn.mean()*100:>9.2f}"
              f"{pf_n:>7.1f}{st['worst']*100:>8.1f}{freq:>10.1f}")
        rows.append((th, st["n"], st["win"], st["mean"], rn.mean(), freq))

    print("\n解读：门槛越低事件越多、频率越高，但期望递减；净期望(扣0.3%)≥3% 视为仍可实盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
