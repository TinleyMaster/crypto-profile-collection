#!/usr/bin/env python3
"""B 档（MID_LONG，30%≤chg24<50% 且 vr≥5.6，N=168h）亏损归因画像。

目的（2026-11-11 第四轮）：B 档累计亏损是 A 档的 4 倍（−1243pp vs −319pp），
是账户最大失血点。本轮只做归因，不做优化——先定位「深亏左尾」还是「阴跌」，
再决定止损/分档/入场过滤的优先级，避免把 A 档的「半仓」结论直接照搬。

数据：data/_green2_events.csv（bin/backtest_momentum_gate.py 已落盘 per-event 缓存，
      5,009 条，含 nl_/lim_ 两口径、MFE、出场原因、入场/出场时刻），vr 从
      data/trade_params_events.csv 按 (symbol, eo) 补齐。
口径：lim（实盘 −0.5% 限价 + 60min 放弃）+ 同币不叠仓；出场 = 实盘 sl10 + EMA7 线破。

输出：
  ① 出场结构（sl / trail / breakeven / expire）：占比、笔均、累计、亏损贡献
  ② chg24 五档（30~35/35~40/40~45/45~50）：均净/PF/止损率/trail笔均/sl笔均/MFE/右尾
  ③ MFE 六档分布：占比、笔均、累计贡献、亏损贡献（对齐 A 档口径）
  ④ vr 三档（5.6~8/8~12/>12）：均净/PF/止损率/右尾
  ⑤ 实际持仓时长：均值/中位、时长桶分布（检验 168h 长周期是否冗余）
  ⑥ 40~45% 异常档的时间切片 OOS（前后半段 + 分年）
  ⑦ A 档同口径对照（判断 B 档亏损机制是否与 A 一致）

用法：python bin/backtest_b_attribution.py [--mode lim|nl]
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_5m_entry import CACHE, COST  # noqa: E402
from backtest_exit_variants import stats  # noqa: E402
from backtest_momentum_gate import load_rows  # noqa: E402

HOUR = 3600
CN = {"sl": "硬止损", "trail": "EMA7线破", "breakeven": "保本", "expire": "到期"}
CHG_BUCKETS = [(0.30, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 0.50)]
MFE_BUCKETS = [(0, 0.03), (0.03, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50), (0.50, 99)]
VR_BUCKETS = [(5.6, 8.0), (8.0, 12.0), (12.0, 1e9)]
HOLD_BUCKETS = [(0, 6), (6, 24), (24, 72), (72, 167), (167, 1e9)]


def load_vr() -> dict[tuple[str, int], float]:
    """(symbol, eo_ts) → 量比 vr（事件缓存里 B 档的入场条件之一）。"""
    out: dict[tuple[str, int], float] = {}
    with CACHE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                v = float(r["vr"] or 0)
            except (TypeError, ValueError):
                continue
            eo = datetime.fromisoformat(r["eo"])
            if eo.tzinfo is None:
                eo = eo.replace(tzinfo=timezone.utc)
            out[(r["symbol"], int(eo.timestamp()))] = v
    return out


def build(rows: list[dict], vr_map: dict, mode: str, tier: str) -> list[dict]:
    """tier='A'/'B'，mode='lim'/'nl'，同币不叠仓（按 sym+tier 贪心）。"""
    picked = []
    for r in rows:
        if r["sig"] != tier:
            continue
        p = r["nl"] if mode == "nl" else r["lim"]
        if p is None:
            continue
        picked.append({
            "ret": p["ret"], "reason": p["reason"], "mfe": p["mfe"],
            "entry": p["entry"], "exit": p["exit"], "sym": r["sym"],
            "chg24": r["chg24"],
            "vr": vr_map.get((r["sym"], int(r["eo"].timestamp())), float("nan")),
            "hold": (p["exit"] - p["entry"]) / HOUR,
            "dt": datetime.fromtimestamp(p["exit"], timezone.utc),
        })
    out, last = [], {}
    for p in sorted(picked, key=lambda x: x["entry"]):
        if p["sym"] in last and p["entry"] < last[p["sym"]]:
            continue
        out.append(p)
        last[p["sym"]] = p["exit"]
    return out


def _net(rows: list[dict]) -> np.ndarray:
    return np.array([r["ret"] - COST for r in rows], float)


def loss_share(sub: list[dict], total_loss: float) -> float:
    """该组净亏损占全样本总亏损的比例。"""
    neg = -_net(sub)
    return 100 * neg[neg > 0].sum() / total_loss if total_loss else 0.0


def section_exit(v: list[dict], tier: str, mode: str) -> None:
    tot_loss = -_net(v)[_net(v) < 0].sum()
    s = stats(v)
    print("\n" + "=" * 112)
    print(f"① 出场结构（{tier} 档，{mode}，同币不叠仓，n={len(v)}）")
    print("=" * 112)
    print(f"  整体：胜={s['win']:.1f}% 均={s['mean']:+.2f}% 中位={s['med']:+.2f}% "
          f"PF={s['PF']:.2f} 累计={s['cum']:+.0f}pp 回撤={s['mdd']:+.0f}pp "
          f"MFE中位={s['mfe']:+.1f}% 回吐中位={s['give']:+.1f}%")
    print(f"  {'出场原因':10s} {'n':>5s} {'占比':>6s} {'笔均净':>8s} {'累计pp':>9s} "
          f"{'亏损贡献':>8s} {'MFE中位':>8s} {'持仓中位h':>9s}")
    for reason, cnt in Counter(r["reason"] for r in v).most_common():
        sub = [r for r in v if r["reason"] == reason]
        net = _net(sub)
        print(f"  {CN.get(reason, reason):10s} {cnt:5d} {100*cnt/len(v):5.1f}% "
              f"{100*net.mean():+8.2f} {100*net.sum():+9.0f} {loss_share(sub, tot_loss):7.1f}% "
              f"{100*np.median([r['mfe'] for r in sub]):+8.1f} "
              f"{np.median([r['hold'] for r in sub]):9.1f}")


def section_chg(v: list[dict], tier: str, mode: str) -> None:
    tot_loss = -_net(v)[_net(v) < 0].sum()
    print("\n" + "=" * 112)
    print(f"② chg24 分档（{tier} 档，{mode}）")
    print("=" * 112)
    print(f"  {'档位':10s} {'n':>5s} {'占比':>6s} {'均净':>7s} {'PF':>6s} {'止损率':>7s} "
          f"{'trail笔均':>9s} {'sl笔均':>8s} {'MFE中位':>8s} {'右尾≥20%':>9s} {'累计pp':>8s}")
    for lo, hi in CHG_BUCKETS:
        sub = [r for r in v if lo <= r["chg24"] < hi]
        if not sub:
            continue
        net = _net(sub)
        gw, gl = net[net > 0].sum(), -net[net < 0].sum()
        tr = [r for r in sub if r["reason"] == "trail"]
        sl = [r for r in sub if r["reason"] == "sl"]
        right = sum(1 for r in sub if r["mfe"] >= 0.20)
        print(f"  {lo*100:.0f}~{hi*100:.0f}%{'':5s} {len(sub):5d} {100*len(sub)/len(v):5.1f}% "
              f"{100*net.mean():+7.2f} {(gw/gl if gl else 9.99):6.2f} "
              f"{100*len(sl)/len(sub):6.1f}% "
              f"{(100*_net(tr).mean() if tr else 0):+9.2f} "
              f"{(100*_net(sl).mean() if sl else 0):+8.2f} "
              f"{100*np.median([r['mfe'] for r in sub]):+8.1f} "
              f"{100*right/len(sub):8.1f}% {100*net.sum():+8.0f}")
    print(f"  注：亏损贡献按档位排序 → "
          + "，".join(
              f"{lo*100:.0f}~{hi*100:.0f}%:{loss_share([r for r in v if lo <= r['chg24'] < hi], tot_loss):.0f}%"
              for lo, hi in CHG_BUCKETS))


def section_mfe(v: list[dict], tier: str, mode: str) -> None:
    tot_loss = -_net(v)[_net(v) < 0].sum()
    print("\n" + "=" * 112)
    print(f"③ MFE 全分布（{tier} 档，{mode}）")
    print("=" * 112)
    print(f"  {'MFE区间':10s} {'n':>5s} {'占比':>6s} {'胜率':>6s} {'笔均净':>8s} "
          f"{'累计pp':>9s} {'亏损贡献':>8s} {'占总盈利':>8s}")
    tot_win = _net(v)[_net(v) > 0].sum()
    for lo, hi in MFE_BUCKETS:
        sub = [r for r in v if lo <= r["mfe"] < hi]
        if not sub:
            continue
        net = _net(sub)
        label = f"{lo*100:.0f}~{hi*100:.0f}%" if hi < 99 else f">{lo*100:.0f}%"
        print(f"  {label:10s} {len(sub):5d} {100*len(sub)/len(v):5.1f}% "
              f"{100*(net > 0).mean():5.1f}% {100*net.mean():+8.2f} "
              f"{100*net.sum():+9.0f} {loss_share(sub, tot_loss):7.1f}% "
              f"{100*net[net > 0].sum()/tot_win if tot_win else 0:7.1f}%")


def section_vr(v: list[dict], tier: str, mode: str) -> None:
    print("\n" + "=" * 112)
    print(f"④ 量比 vr 分档（{tier} 档，{mode}）")
    print("=" * 112)
    print(f"  {'vr区间':10s} {'n':>5s} {'占比':>6s} {'均净':>7s} {'PF':>6s} {'止损率':>7s} "
          f"{'MFE中位':>8s} {'右尾≥20%':>9s} {'累计pp':>8s}")
    for lo, hi in VR_BUCKETS:
        sub = [r for r in v if lo <= r["vr"] < hi]
        if not sub:
            continue
        net = _net(sub)
        gw, gl = net[net > 0].sum(), -net[net < 0].sum()
        sl = sum(1 for r in sub if r["reason"] == "sl")
        right = sum(1 for r in sub if r["mfe"] >= 0.20)
        label = f"{lo:.1f}~{hi:.0f}" if hi < 1e8 else f">{lo:.0f}"
        print(f"  {label:10s} {len(sub):5d} {100*len(sub)/len(v):5.1f}% "
              f"{100*net.mean():+7.2f} {(gw/gl if gl else 9.99):6.2f} "
              f"{100*sl/len(sub):6.1f}% {100*np.median([r['mfe'] for r in sub]):+8.1f} "
              f"{100*right/len(sub):8.1f}% {100*net.sum():+8.0f}")


def section_hold(v: list[dict], tier: str, mode: str) -> None:
    print("\n" + "=" * 112)
    print(f"⑤ 实际持仓时长（{tier} 档，{mode}，计划 N=168h）")
    print("=" * 112)
    holds = np.array([r["hold"] for r in v], float)
    tr = [r["hold"] for r in v if r["reason"] == "trail"]
    print(f"  均值={holds.mean():.1f}h 中位={np.median(holds):.1f}h "
          f"p90={np.percentile(holds, 90):.1f}h 最长={holds.max():.1f}h；"
          f"trail 出场中位={np.median(tr) if tr else 0:.1f}h")
    print(f"  {'时长桶':10s} {'n':>5s} {'占比':>6s} {'均净':>7s} {'PF':>6s} {'累计pp':>8s}")
    for lo, hi in HOLD_BUCKETS:
        sub = [r for r in v if lo <= r["hold"] < hi]
        if not sub:
            continue
        net = _net(sub)
        gw, gl = net[net > 0].sum(), -net[net < 0].sum()
        label = f"{lo:.0f}~{hi:.0f}h" if hi < 1e8 else f">{lo:.0f}h"
        print(f"  {label:10s} {len(sub):5d} {100*len(sub)/len(v):5.1f}% "
              f"{100*net.mean():+7.2f} {(gw/gl if gl else 9.99):6.2f} {100*net.sum():+8.0f}")


def section_oos(v: list[dict], tier: str, mode: str) -> None:
    """40~45% 异常档：前后半段 + 分年，检验是否样本内巧合。"""
    mid = sorted(r["dt"] for r in v)[len(v) // 2]
    halves = [("前半段", [r for r in v if r["dt"] < mid]),
              ("后半段", [r for r in v if r["dt"] >= mid])]
    print("\n" + "=" * 112)
    print(f"⑥ 40~45% 异常档 OOS 切片（{tier} 档，{mode}）")
    print("=" * 112)
    print(f"  {'切片':12s} {'档位':10s} {'n':>5s} {'均净':>7s} {'PF':>6s} {'止损率':>7s} {'累计pp':>8s}")
    groups = halves + [(f"分年{y}", [r for r in v if r["dt"].year == y])
                       for y in sorted({r["dt"].year for r in v})]
    for name, sub_all in groups:
        for lo, hi in ((0.40, 0.45), (0.30, 0.40), (0.45, 0.50)):
            sub = [r for r in sub_all if lo <= r["chg24"] < hi]
            if not sub:
                continue
            net = _net(sub)
            gw, gl = net[net > 0].sum(), -net[net < 0].sum()
            sl = sum(1 for r in sub if r["reason"] == "sl")
            print(f"  {name:12s} {lo*100:.0f}~{hi*100:.0f}%{'':5s} {len(sub):5d} "
                  f"{100*net.mean():+7.2f} {(gw/gl if gl else 9.99):6.2f} "
                  f"{100*sl/len(sub):6.1f}% {100*net.sum():+8.0f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("lim", "nl"), default="lim")
    args = ap.parse_args()

    rows = load_rows()
    vr_map = load_vr()
    b = build(rows, vr_map, args.mode, "B")
    a = build(rows, vr_map, args.mode, "A")
    print(f"[load] 事件 {len(rows):,}；B 档（同币不叠仓）{len(b):,}；A 档对照 {len(a):,}")

    section_exit(b, "B", args.mode)
    section_chg(b, "B", args.mode)
    section_mfe(b, "B", args.mode)
    section_vr(b, "B", args.mode)
    section_hold(b, "B", args.mode)
    section_oos(b, "B", args.mode)
    section_exit(a, "A", args.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
