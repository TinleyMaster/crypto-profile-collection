#!/usr/bin/env python3
"""A/B/C 信号 × vr 五分位分档回测（事件池内横截面排序）。

背景结论（用户 2026-10-07 分档回测）：
  涨幅榜（24h 涨幅>0）内按缩量排序，后续收益单调递减——Q1 最缩量最好、
  Q5 最放量明确负信号（12h H72 中位 -2.9%、胜率 38%）。
  但我们的 A/B 信号要求 vr≥2（放量端），需验证：信号池内 vr 分档单调性是否成立，
  尤其 B（20~50% 中低涨幅）是否正是「中低涨幅+高放量=诱多」的重灾区（vr 反向分档）。

口径（2026-10-07）：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv，全周期
  - 出场：A/B 用 TRAIL3（ph_tr3×0.97/entry-1）；C 用 FIX 12h/TP50/SL10；成本 0.3%
  - 分档：各信号池内按 vr 分 5 档（Q1 最缩量 ~ Q5 最放量，池内分位数）
  - 检验：Q1→Q5 期望单调性；Q5（最放量）是否为负信号；B 低 vr 子集能否改善

用法：python bin/backtest_vr_quantiles.py
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

from backtest_trade_params import CACHE, _returns_vec, _vec_rows, load_events  # noqa: E402

TR = 0.03
COST = 0.003
MIN_N = 50


def _stats(ret):
    r = ret[~np.isnan(ret)]
    if len(r) < MIN_N:
        return None
    rn = r - COST
    win = rn > 0
    gw, gl = float(rn[win].sum()), abs(float(rn[~win].sum())) if (~win).any() else 0.0
    return {"n": len(r), "win": float((rn > 0).mean()), "mean": float(rn.mean()),
            "median": float(np.median(rn)), "pf": gw / gl if gl > 0 else float("inf"),
            "worst": float(np.min(rn))}


def report(mask, ret, label):
    st = _stats(np.where(mask, ret, np.nan))
    if st is None:
        print(f"  {label:<26} n<{MIN_N}")
        return None
    print(f"  {label:<26} n={st['n']:>6,} 胜率={st['win']*100:>6.1f}% 期望={st['mean']*100:>+7.2f}% "
          f"中位={st['median']*100:>+6.2f}% PF={st['pf']:>5.1f} 最差={st['worst']*100:>+6.1f}%")
    return st


def run_signal(rows, arr, chg, label, mask, ret, vr):
    n_total = int(mask.sum())
    if n_total < MIN_N * 3:
        print(f"\n■ {label}  样本不足({n_total})")
        return
    print(f"\n■ {label}（n={n_total:,}，池内 vr 五分位）")
    report(mask, ret, "全池基准")
    v = vr[mask]
    qs = np.quantile(v, [0.2, 0.4, 0.6, 0.8])
    bounds = [f"{qs[0]:.2f}", f"{qs[1]:.2f}", f"{qs[2]:.2f}", f"{qs[3]:.2f}"]
    means = []
    for i, (lo, hi) in enumerate(zip([-np.inf] + list(qs), list(qs) + [np.inf])):
        m = mask & (vr >= lo) & (vr < hi)
        st = report(m, ret, f"Q{i+1} vr[{lo:.2f}~{hi:.2f})")
        means.append(st["mean"] if st else np.nan)
    # 单调性与差值
    valid = [x for x in means if x == x]
    mono = all(valid[i] >= valid[i + 1] for i in range(len(valid) - 1))
    d = (means[0] - means[-1]) * 100 if means[0] == means[0] and means[-1] == means[-1] else float("nan")
    print(f"  Q1→Q5 期望变化: {[f'{x*100:+.2f}%' if x==x else 'n/a' for x in means]}")
    print(f"  单调递减(Q1最好,Q5最差): {'✅' if mono else '❌'}   Q1-Q5 差值: {d:+.2f}pp")


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    vr = np.array([r["vr"] for r in rows], dtype=float)
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    ret_c = _returns_vec(arr, -1, 12, 0.50, 0.10, None)

    run_signal(rows, arr, chg, "A 信号（chg24≥50% 做多）", chg >= 0.50, ret_ab, vr)
    run_signal(rows, arr, chg, "B 信号（20~50% 做多）", (0.20 <= chg) & (chg < 0.50), ret_ab, vr)
    run_signal(rows, arr, chg, "C 信号（<5% 做空）", chg < 0.05, ret_c, vr)

    # B 信号 vr 细分：低 vr(2~2.6) vs 高 vr
    b = (0.20 <= chg) & (chg < 0.50)
    print("\n■ B 信号 vr 阈值细分（对照用户 Q5≈vr>2.58）")
    report(b & (vr < 2.6), ret_ab, "B · vr 2.0~2.6")
    report(b & (vr >= 2.6) & (vr < 4.0), ret_ab, "B · vr 2.6~4.0")
    report(b & (vr >= 4.0), ret_ab, "B · vr ≥4.0")
    report(b & (vr < 3.0), ret_ab, "B · vr <3.0（剔除高放量）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
