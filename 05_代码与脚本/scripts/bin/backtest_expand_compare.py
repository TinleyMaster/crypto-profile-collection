#!/usr/bin/env python3
"""扩容取舍：A 降门槛(40%) vs B 右上角 — 完整对比。

问题：A(≥50%) 信号稀缺(实盘2~5天/笔)。两条扩容路径：
  路径甲：A 门槛 50% → 40%（扩大事件池）
  路径乙：并行 B 右上角（chg24 20~50% 内 涨幅+vr 组合排序前20%）
两者在 chg24 40~50% 区间可能重叠，需量化重叠度与收益/风险取舍。

口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - 出场：TRAIL3（ph_tr3×0.97/entry-1）；成本 0.3%
  - 净值：30% 固定仓位线性累加（与 backtest_a_metrics 一致）；年化 365 天
  - 输出：① 门槛扫描完整指标；② B 右上角指标；③ 三实盘配置年化/回撤/频率；④ 重叠分析

用法：python bin/backtest_expand_compare.py
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

from backtest_trade_params import _vec_rows, load_events  # noqa: E402

TR = 0.03
COST = 0.003
W = 0.30        # 固定仓位
DAYS = 365.0    # 年化口径


def _net(ret):
    return np.where(~np.isnan(ret), ret - COST, np.nan)


def _stats(mask, ret):
    r = _net(ret)[mask]
    r = r[~np.isnan(r)]
    if len(r) < 50:
        return None
    win = r > 0
    gw, gl = float(r[win].sum()), abs(float(r[~win].sum())) if (~win).any() else 0.0
    return {"n": int(len(r)), "win": float((r > 0).mean()), "mean": float(r.mean()),
            "median": float(np.median(r)), "pf": gw / gl if gl > 0 else float("inf"),
            "worst": float(np.min(r))}


def _equity_metrics(mask, ret, eo_days):
    """30% 仓位线性净值 → 总收益/年化/最大回撤/回撤修复天数。eo_days: 每事件距起点天数。"""
    r = _net(ret)[mask & ~np.isnan(ret)]
    idx = np.where(mask & ~np.isnan(ret))[0]
    if len(idx) < 10:
        return None
    order = np.argsort(eo_days[idx])
    r = r[order]
    t = eo_days[idx][order]
    eq = np.cumsum(W * r)                       # 线性净值（从 0 起）
    peak = np.maximum.accumulate(eq)
    dd = eq - peak                              # 回撤（负）
    max_dd = float(dd.min()) if len(dd) else 0.0
    total = float(eq[-1])
    span_days = float(t[-1] - t[0]) if len(t) > 1 else 1.0
    ann = (1 + total) ** (DAYS / max(span_days, 1.0)) - 1 if total > -1 else -1.0
    # 回撤修复：最大回撤谷底 → 回到前高的天数
    repair = None
    if max_dd < 0:
        trough = int(np.argmin(dd))
        hi = eq[:trough + 1].max()
        after = eq[trough:]
        rec = np.where(after >= hi)[0]
        if len(rec):
            repair = int(t[trough + rec[0]] - t[trough]) if trough + rec[0] < len(t) else None
    freq = len(idx) / (span_days / DAYS) if span_days > 0 else 0
    return {"total": total, "ann": ann, "max_dd": max_dd, "repair": repair, "freq_per_year": freq}


def _pct_rank(v, mask):
    sub = v[mask]
    out = np.full(len(v), np.nan)
    if len(sub) > 1:
        out[mask] = sub.argsort().argsort() / max(len(sub) - 1, 1)
    return out


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    vr = np.array([r["vr"] for r in rows], dtype=float)
    eo = np.array([r["eo"] for r in rows])
    ret = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    eo_days = (eo - eo.min()).astype("timedelta64[D]").astype(float)

    print("=" * 100)
    print("① chg24 门槛扫描（TRAIL3，含 0.3% 成本，30% 仓位净值）")
    print("=" * 100)
    for th in (0.30, 0.35, 0.40, 0.45, 0.50):
        m = chg >= th
        st = _stats(m, ret)
        eq = _equity_metrics(m, ret, eo_days)
        if not st or not eq:
            print(f"  ≥{th*100:.0f}%  n<50")
            continue
        print(f"  ≥{th*100:.0f}%  n={st['n']:>6,} 胜率={st['win']*100:>5.1f}% "
              f"期望={st['mean']*100:>+6.2f}% PF={st['pf']:>5.1f} 中位={st['median']*100:>+5.2f}% "
              f"最差={st['worst']*100:>+5.1f}% | 年化={eq['ann']*100:>+7.1f}% "
              f"回撤={eq['max_dd']*100:>+6.1f}% 修复={eq['repair']}天 频率={eq['freq_per_year']:.0f}笔/年")

    # B 右上角：chg24 20~50% 内 涨幅+vr 组合 rank≥0.8
    b = (0.20 <= chg) & (chg < 0.50)
    s2 = (_pct_rank(chg, b) + _pct_rank(vr, b)) / 2
    bq5 = b & (s2 >= 0.8)
    print("\n" + "=" * 100)
    print("② B 右上角（chg24 20~50% 内 涨幅+vr 组合排序前20%）")
    print("=" * 100)
    st = _stats(bq5, ret)
    eq = _equity_metrics(bq5, ret, eo_days)
    print(f"  B右上角  n={st['n']:>6,} 胜率={st['win']*100:>5.1f}% 期望={st['mean']*100:>+6.2f}% "
          f"PF={st['pf']:>5.1f} 中位={st['median']*100:>+5.2f}% 最差={st['worst']*100:>+5.1f}% | "
          f"年化={eq['ann']*100:>+7.1f}% 回撤={eq['max_dd']*100:>+6.1f}% 修复={eq['repair']}天 "
          f"频率={eq['freq_per_year']:.0f}笔/年")
    print(f"  B右上角 实际区间: chg24[{chg[bq5].min()*100:.0f}%,{chg[bq5].max()*100:.0f}%] "
          f"vr[{vr[bq5].min():.1f}~{vr[bq5].max():.0f}]")

    # ③ 三实盘配置
    print("\n" + "=" * 100)
    print("③ 三种实盘配置对比")
    print("=" * 100)
    cfgs = [
        ("C1 现状：A≥50%", chg >= 0.50),
        ("C2 降门槛：A≥40%", chg >= 0.40),
        ("C3 并行：A≥50% ∪ B右上角", (chg >= 0.50) | bq5),
    ]
    for name, m in cfgs:
        st = _stats(m, ret)
        eq = _equity_metrics(m, ret, eo_days)
        print(f"  {name:<26} n={st['n']:>6,} 期望={st['mean']*100:>+6.2f}% PF={st['pf']:>5.1f} | "
              f"年化={eq['ann']*100:>+7.1f}% 回撤={eq['max_dd']*100:>+6.1f}% 修复={eq['repair']}天 "
              f"频率={eq['freq_per_year']:.0f}笔/年")

    # ④ 重叠分析
    print("\n" + "=" * 100)
    print("④ 重叠分析（C3 的 B 右上角部分 vs 降门槛 40%）")
    print("=" * 100)
    a50 = chg >= 0.50
    a40 = chg >= 0.40
    br = bq5
    new_br = br & ~a50                     # B右上角里非 A50 的（纯新增）
    inter = new_br & a40                   # 新增 ∩ 40~50%
    n_new, n_inter = int(new_br.sum()), int(inter.sum())
    print(f"  C3 中 B 右上角事件: {int(br.sum()):,}")
    print(f"  其中非 A50 纯新增: {n_new:,}")
    print(f"  新增 ∩ chg40~50% : {n_inter:,}（占新增 {n_inter/max(n_new,1)*100:.0f}%）")
    print(f"  → 降门槛 40% 与 B右上角重叠度："
          f"B右上角中 {int((br & a40).sum())/max(int(br.sum()),1)*100:.0f}% 落在 40~50% 区间")
    # B右上角 vs ≥40 的并集
    union = a40 | br
    st_u = _stats(union, ret)
    eq_u = _equity_metrics(union, ret, eo_days)
    print(f"  若 C2+C3 并集(≥40% ∪ B右上角): n={st_u['n']:,} 期望={st_u['mean']*100:+.2f}% "
          f"PF={st_u['pf']:.1f} 年化={eq_u['ann']*100:+.1f}% 回撤={eq_u['max_dd']*100:+.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
