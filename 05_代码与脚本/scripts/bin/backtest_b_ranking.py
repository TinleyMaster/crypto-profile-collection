#!/usr/bin/env python3
"""B 信号（20~50% 做多）内部综合排序分档回测：涨跌幅 + vr 越高越靠前。

背景：
  - B 全池 TRAIL3 净期望 +2.00%（远低于 A 的 +5.31%）
  - 前轮发现 B 内 vr 单调递增（Q1 +1.52% → Q5 +3.00%）
  - 本轮：把「涨幅(chg24) + 量比(vr)」组合排序（rank 平均，越高越靠前），
    看高排序档（高涨幅高放量）是否单调更好、能否逼近 A。

口径（2026-10-07）：
  - 事件：放量大阳（chg1h≥3% & vr≥2）且 chg24∈[20%,50%)，缓存 trade_params_events.csv
  - 出场：TRAIL3（ph_tr3×0.97/entry-1）；成本 0.3%
  - 排序：chg24 / vr / 组合(rank 平均) 各按 B 池内分 5 档（Q1 最低 ~ Q5 最高）
  - 对照：A 信号全池基准 +5.31%

用法：python bin/backtest_b_ranking.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from backtest_trade_params import CACHE, _vec_rows, load_events  # noqa: E402

TR = 0.03
COST = 0.003
MIN_N = 100


def _pct_rank(v: np.ndarray, mask) -> np.ndarray:
    """B 池内百分位排名（0~1，越大越靠前）。"""
    sub = v[mask]
    order = sub.argsort().argsort()
    ranks = order / max(len(sub) - 1, 1)
    out = np.full(len(v), np.nan)
    out[mask] = ranks
    return out


def _stats(ret):
    r = ret[~np.isnan(ret)]
    if len(r) < MIN_N:
        return None
    rn = r - COST
    win = rn > 0
    gw, gl = float(rn[win].sum()), abs(float(rn[~win].sum())) if (~win).any() else 0.0
    return {"n": len(r), "win": float((rn > 0).mean()), "mean": float(rn.mean()),
            "median": float(np.median(rn)), "pf": gw / gl if gl > 0 else float("inf")}


def run_sort(rows, arr, chg, vr, ret, b, label):
    print(f"\n■ B 信号 × {label}（B 池内排序，越高越靠前）")
    q = _pct_rank(chg, b) if "涨幅" in label and "组合" not in label else None
    if label == "涨幅(chg24)":
        s = _pct_rank(chg, b)
    elif label == "量比(vr)":
        s = _pct_rank(vr, b)
    else:
        s = (_pct_rank(chg, b) + _pct_rank(vr, b)) / 2
    means = []
    for i in range(5):
        lo, hi = i / 5, (i + 1) / 5
        m = b & (s >= lo) & (s < hi)
        st = _stats(np.where(m, ret, np.nan))
        if st:
            means.append(st["mean"])
            print(f"  Q{i+1}({'最低' if i==0 else '最高' if i==4 else f'{int(lo*100)}~{int(hi*100)}pct'}) "
                  f"n={st['n']:>6,} 胜率={st['win']*100:>6.1f}% 期望={st['mean']*100:>+7.2f}% "
                  f"中位={st['median']*100:>+6.2f}% PF={st['pf']:>5.1f}")
        else:
            means.append(np.nan)
    valid = [x for x in means if x == x]
    mono_up = all(valid[i] <= valid[i + 1] for i in range(len(valid) - 1))
    print(f"  单调递增(Q1低→Q5高): {'✅' if mono_up else '❌'}  "
          f"Q5-Q1 = {((means[-1] - means[0]) * 100 if means[-1]==means[-1] and means[0]==means[0] else float('nan')):+.2f}pp")


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    vr = np.array([r["vr"] for r in rows], dtype=float)
    ret = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    b = (0.20 <= chg) & (chg < 0.50)

    print("B 信号全池基准:")
    st = _stats(np.where(b, ret, np.nan))
    print(f"  n={st['n']:,} 期望={st['mean']*100:+.2f}% PF={st['pf']:.1f}")
    print(f"A 信号对照: +5.31% / PF 11.3")

    run_sort(rows, arr, chg, vr, ret, b, "涨幅(chg24)")
    run_sort(rows, arr, chg, vr, ret, b, "量比(vr)")
    run_sort(rows, arr, chg, vr, ret, b, "组合(涨幅+vr rank平均)")

    # 组合排序 Q5 与 A 对比 + 更细的头部
    s = (_pct_rank(chg, b) + _pct_rank(vr, b)) / 2
    print("\n■ 组合排序头部细分（能否逼近 A）")
    for thr, label in ((0.8, "Q5 头部(前20%)"), (0.9, "前10%"), (0.95, "前5%")):
        m = b & (s >= thr)
        st = _stats(np.where(m, ret, np.nan))
        if st:
            print(f"  {label:<14} n={st['n']:>6,} 胜率={st['win']*100:>6.1f}% "
                  f"期望={st['mean']*100:>+7.2f}% 中位={st['median']*100:>+6.2f}% PF={st['pf']:>5.1f}")
    # 组合排序 Q5 内的实际 chg24/vr 边界
    m = b & (s >= 0.8)
    print(f"  Q5 实际区间: chg24[{chg[m].min()*100:.0f}%,{chg[m].max()*100:.0f}%] "
          f"vr[{vr[m].min():.1f}~{vr[m].max():.1f}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
