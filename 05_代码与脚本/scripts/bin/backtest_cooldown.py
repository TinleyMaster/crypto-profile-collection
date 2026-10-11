#!/usr/bin/env python3
"""冷却时间回测：同币同信号类型的「信号发射层去重」窗口 C 扫描。

复刻线上 scan_gainers_v2.py 的冷却口径（COOLDOWN_H=12，「同 symbol 同信号类型最近
12h 已出信号则跳过」）：每币按时间排序，取一条信号后，同币同类型的后续信号在 C 小时
内一律跳过；**被跳过的信号不重置计时器**（计时从「上一次实际保留的信号」算起）。

对 C ∈ {0,6,12,24,48,72,96,168}h 输出：保留笔数 / 剔除率 / 总 PnL / 单笔均值 / PF / 胜率，
用于判断更长冷却是否以「少做单」换来「单笔更优」，以及总量是否可接受。

出场与 backtest_divergence.py 一致：A/B 做多用 TRAIL3，含 0.3% 成本。
参考线上值：scan_gainers_v2 COOLDOWN_H=12（扫描层）；KOL 链路 SIGNAL_COOLDOWN_MINUTES=30(0.5h)。

用法：python bin/backtest_cooldown.py [--start 2023-01-01]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import pandas as pd  # noqa: E402

from backtest_trade_params import _vec_rows, load_events  # noqa: E402

TR = 0.03
COST = 0.003
MIN_N = 30
C_LIST = [0, 6, 12, 24, 48, 72, 96, 168]


def _groups(order, mask):
    """按 symbol 分组、组内按时间升序的索引列表（仅保留 mask 内的事件）。"""
    out = []
    for g in order:
        f = [i for i in g if mask[i]]
        if f:
            out.append(f)
    return out


def _cooldown_keep(groups, eo, C):
    """同币同类型冷却 C 小时：保留首信号，跳过 C 小时内后续信号（跳过的重置计时器）。"""
    kept = []
    gap = C * 3600.0
    for g in groups:
        last = -1e18
        for i in g:
            if eo[i] - last >= gap:
                kept.append(i)
                last = eo[i]
    return kept


def _metrics(idxs, ret_net):
    r = np.asarray([ret_net[i] for i in idxs if ret_net[i] == ret_net[i]], dtype=float)
    if len(r) < MIN_N:
        return None
    win = r > 0
    gw, gl = float(r[win].sum()), abs(float(r[~win].sum())) if (~win).any() else 0.0
    return {"n": len(r), "total": float(r.sum()), "mean": float(r.mean()),
            "median": float(np.median(r)), "win": float(win.mean()),
            "pf": gw / gl if gl > 0 else float("inf")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    args = ap.parse_args()
    start = args.start
    rows = load_events()
    rows = [r for r in rows if r["eo"] >= pd.Timestamp(start, tz="UTC")]
    print(f"事件子集: {len(rows):,}（{start} 起）")
    arr = _vec_rows(rows)
    chg, eo = arr["chg24"], arr["eo"]
    ret_net = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1 - COST   # TRAIL3 净收益

    order_all = sorted(range(len(rows)), key=lambda i: (rows[i]["symbol"], eo[i]))
    order, cur, buf = [], None, []
    for i in order_all:
        s = rows[i]["symbol"]
        if s != cur:
            if buf:
                order.append(buf)
            cur, buf = s, []
        buf.append(i)
    if buf:
        order.append(buf)

    sigs = {"A 做多(chg24≥50%)": chg >= 0.50,
            "B 做多(20~50%)": (0.20 <= chg) & (chg < 0.50)}

    print("=" * 104)
    print("冷却时间扫描（同币同类型的信号发射层去重；TRAIL3 净收益，含 0.3% 成本）")
    print("=" * 104)
    for sig, m in sigs.items():
        groups = _groups(order, m)
        base = _metrics([i for g in groups for i in g], ret_net)
        if base is None:
            print(f"\n■ {sig}: 样本不足")
            continue
        print(f"\n■ {sig} | 无冷却基线 笔={base['n']:,} 总PnL={base['total']*100:+,.0f}pp "
              f"单笔={base['mean']*100:+.2f}% PF={base['pf']:.1f} 胜={base['win']*100:.1f}%")
        print(f"  {'冷却C(h)':>8} {'笔数':>7} {'剔除率':>7} {'总PnL(pp)':>11} "
              f"{'单笔':>7} {'中位':>7} {'PF':>6} {'胜率':>6}  {'':<12}")
        for C in C_LIST:
            kept = _cooldown_keep(groups, eo, C)
            st = _metrics(kept, ret_net)
            if st is None:
                print(f"  {C:>8} 样本不足")
                continue
            cut = (1 - st["n"] / base["n"]) * 100
            tag = "  ← 线上扫描值" if C == 12 else ""
            print(f"  {C:>8} {st['n']:>7,} {cut:>6.1f}% {st['total']*100:>+11,.0f} "
                  f"{st['mean']*100:>+6.2f}% {st['median']*100:>+6.2f}% {st['pf']:>6.1f} "
                  f"{st['win']*100:>5.1f}%{tag}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())