#!/usr/bin/env python3
"""动量闸门 / 连续收阳（green2）的入场端单变量回测 + OOS 加固验证。

背景（2026-11-11 第三轮）：出场端「提前收紧」已全部证伪（PF 0.90→0.85），
唯一有效的是收缩左尾（SL 15%→10%）。入场端两轮结论：
  - chg24 分档与期望不单调（50~75% 亏最多，75~150% 反而正）
  - ret30 阈值收紧无单调性（噪声）
  - 唯一正信号：A 档「连续 2 根 5m 收阳」（green2），但未做 OOS / 未叠实盘约束

本脚本按用户要求做三层加固：
  ① OOS：时间前后半段、分年、币种随机对拆
  ② 实盘约束：-0.5% 限价回撤入场（60min 未成交即放弃）+ 同币不叠仓
  ③ 归因：拦截组是否集中在「MFE<5% + EMA7 阴跌」的伪趋势；保留组右尾是否被削

方法（单变量：只改入场过滤，出场固定当前实盘 SL10 + EMA7 线破 + 到期）：
  事件源  data/trade_params_events.csv
  5m 特征 F:\\代码\\coindata\\coin-binance-swap-candle-5m-csv-2026-11-08
          sig = 事件 bar 收盘时刻；ret30 = close(sig)/close(sig−30min)−1
          last5 = 最近一根 5m 涨幅；green2 = 最近两根 5m 均收阳
  收益    1h 本地 CSV + backtest_exit_variants.simulate（cfg = sl10）

两个口径同时落盘：
  nl_*  = 纯信号池（事件 bar 收盘价入场，不做同币压制）
  lim_* = 实盘口径（-0.5% 限价、60min 有效、同币不叠仓在分析阶段施加）

用法：
  python bin/backtest_momentum_gate.py              # 全量扫描（约 20min），落盘后分析
  python bin/backtest_momentum_gate.py --reuse      # 复用已落盘 CSV，只跑分析
  python bin/backtest_momentum_gate.py --limit 60   # 小样本自测
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_5m_entry import (  # noqa: E402
    CACHE, COST, DATA_1H, DATA_5M, HOUR, csv_sym, load_events, read_klines,
)
from backtest_exit_variants import load_chg24, simulate, stats  # noqa: E402

BAR5 = 300
CFG = {"sl_pct": 0.10}          # 当前实盘出场（SL 10% + EMA7 线破 + 到期）
LIMIT_DISCOUNT = 0.005          # 实盘限价入场：信号价 −0.5%
LIMIT_WAIT = HOUR               # 挂单 60min 未成交即放弃
OUT_CSV = SCRIPT_DIR.parent / "data" / "_green2_events.csv"

# 闸门：name → (ret30 门槛, 是否允许 last5>2% 兜底, 是否要求 green2)
GATES: dict[str, tuple[float, bool, bool]] = {
    "G0_现行": (0.000, True, False),
    "G5_green2": (0.000, False, True),
}


def features(f5: dict | None, sig: int) -> dict | None:
    """sig 时刻的 5m 结构特征；数据缺口返回 None。"""
    if f5 is None:
        return None
    t = f5["t"]
    i_end = int(np.searchsorted(t, sig - BAR5, side="left"))
    i_beg = int(np.searchsorted(t, sig - 2100, side="left"))
    if (i_end >= len(t) or t[i_end] != sig - BAR5
            or i_beg >= len(t) or t[i_beg] != sig - 2100
            or i_end < 1 or t[i_end - 1] != sig - 2 * BAR5):
        return None
    c, o = f5["c"], f5["o"]
    return {
        "c_sig": float(c[i_end]),
        "ret30": float(c[i_end] / c[i_beg] - 1),
        "last5": float(c[i_end] / c[i_end - 1] - 1),
        "green2": int(c[i_end] > o[i_end] and c[i_end - 1] > o[i_end - 1]),
    }


def find_fill(f5: dict, sig: int, limit: float) -> int | None:
    """sig 之后 60min 内首次触及 limit 的 bar 收盘时刻（无成交返回 None）。"""
    t, l = f5["t"], f5["l"]
    i0 = int(np.searchsorted(t, sig, side="left"))
    for j in range(i0, min(i0 + 12, len(t))):
        if t[j] >= sig + LIMIT_WAIT:
            break
        if l[j] <= limit:
            return int(t[j]) + BAR5
    return None


# --------------------------------------------------------------------------- 抽取
def extract(limit_n: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    events = [e for e in load_events()
              if e["eo"] + timedelta(hours=e["n_h"] + 2) <= now]
    if limit_n:
        events = events[:limit_n]
    print(f"[events] {len(events):,} 条  A={sum(e['sig'] == 'A' for e in events):,}"
          f" B={sum(e['sig'] == 'B' for e in events):,}", flush=True)

    by_sym: dict[str, list] = defaultdict(list)
    for e in events:
        by_sym[e["sym"]].append(e)

    chg24 = load_chg24()
    rows: list[dict] = []
    miss_f = miss_ft = miss_out = 0
    for k, (sym, evs) in enumerate(by_sym.items(), 1):
        h1 = read_klines(DATA_1H / f"{csv_sym(sym)}.csv")
        f5 = read_klines(DATA_5M / f"{csv_sym(sym)}.csv")
        if h1 is None or f5 is None:
            miss_f += len(evs)
            continue
        for ev in evs:
            eo_ts = int(ev["eo"].timestamp())
            ft = features(f5, eo_ts + HOUR)
            if ft is None:
                miss_ft += 1
                continue
            i_eo = int(np.searchsorted(h1["t"], eo_ts, side="left"))
            if i_eo >= len(h1["t"]) or h1["t"][i_eo] != eo_ts:
                miss_ft += 1
                continue
            out = simulate(h1, i_eo, ev["entry"], ev["n_h"], CFG, ev["sig"])
            if out is None:
                miss_out += 1
                continue
            ret, reason, hold, mfe = out
            rec = {
                "sym": sym, "sig": ev["sig"], "eo": eo_ts,
                "chg24": chg24.get((sym, eo_ts), ""),
                **ft,
                "nl_ret": ret, "nl_reason": reason, "nl_mfe": mfe,
                "nl_entry": eo_ts + HOUR,
                "nl_exit": int(eo_ts + HOUR + hold * HOUR),
                "lim_filled": 0, "lim_ret": "", "lim_reason": "",
                "lim_mfe": "", "lim_entry": "", "lim_exit": "",
            }
            lim = ft["c_sig"] * (1 - LIMIT_DISCOUNT)
            fill_ts = find_fill(f5, eo_ts + HOUR, lim)
            if fill_ts is not None:
                o2 = simulate(h1, i_eo, lim, ev["n_h"], CFG, ev["sig"])
                if o2 is not None:
                    r2, rs2, h2, m2 = o2
                    rec.update(lim_filled=1, lim_ret=r2, lim_reason=rs2,
                               lim_mfe=m2, lim_entry=fill_ts,
                               lim_exit=int(eo_ts + HOUR + h2 * HOUR))
            rows.append(rec)
        if k % 50 == 0:
            print(f"  ... {k}/{len(by_sym)} 币，有效 {len(rows):,}", flush=True)

    print(f"[data] 缺文件事件 {miss_f:,}；特征缺口 {miss_ft:,}；"
          f"收益不可算 {miss_out:,}；有效 {len(rows):,}；"
          f"限价成交 {sum(r['lim_filled'] for r in rows):,}", flush=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"[out] {OUT_CSV}", flush=True)
    return load_rows()


# --------------------------------------------------------------------------- 载入
def load_rows() -> list[dict]:
    out: list[dict] = []
    with OUT_CSV.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rec = {
                "sym": r["sym"], "sig": r["sig"],
                "eo": datetime.fromtimestamp(int(r["eo"]), timezone.utc),
                "chg24": float(r["chg24"]) if r["chg24"] else float("nan"),
                "ret30": float(r["ret30"]), "last5": float(r["last5"]),
                "green2": r["green2"] == "1",
                "nl": {"ret": float(r["nl_ret"]), "reason": r["nl_reason"],
                       "mfe": float(r["nl_mfe"]),
                       "entry": int(r["nl_entry"]), "exit": int(r["nl_exit"])},
                "lim": None,
            }
            if r["lim_filled"] == "1":
                rec["lim"] = {"ret": float(r["lim_ret"]), "reason": r["lim_reason"],
                              "mfe": float(r["lim_mfe"]),
                              "entry": int(r["lim_entry"]), "exit": int(r["lim_exit"])}
            out.append(rec)
    return out


# --------------------------------------------------------------------------- 分析
def view(rows: list[dict], mode: str, no_overlap: bool = False) -> list[dict]:
    """mode: 'nl' 纯信号池 / 'lim' 实盘（仅已成交）；返回带 dt 的 stats 行。"""
    picked = []
    for r in rows:
        p = r["nl"] if mode == "nl" else r["lim"]
        if p is None:
            continue
        picked.append({"ret": p["ret"], "reason": p["reason"], "mfe": p["mfe"],
                       "sig": r["sig"], "chg24": r["chg24"],
                       "ret30": r["ret30"], "last5": r["last5"],
                       "green2": r["green2"],
                       "entry": p["entry"], "exit": p["exit"], "sym": r["sym"],
                       "dt": datetime.fromtimestamp(p["exit"], timezone.utc)})
    if not no_overlap:
        return picked
    out, last = [], {}
    for p in sorted(picked, key=lambda x: x["entry"]):
        k = (p["sym"], p["sig"])
        if k in last and p["entry"] < last[k]:
            continue
        out.append(p)
        last[k] = p["exit"]
    return out


def gate(rows: list[dict], name: str) -> list[dict]:
    thr, use_last5, need_g2 = GATES[name]
    return [r for r in rows
            if (r["ret30"] >= thr or (use_last5 and r["last5"] > 0.02))
            and (r["green2"] if need_g2 else True)]


def wstats(rows: list[dict], weight=None) -> dict | None:
    """带仓位权重的净收益统计（累计 pp / 回撤按权重缩放）。"""
    if not rows:
        return None
    ws = np.array([weight(r) if weight else 1.0 for r in rows], float)
    net = np.array([r["ret"] - COST for r in rows]) * ws
    win = net > 0
    gw, gl = net[win].sum(), -net[~win].sum()
    eq = peak = mdd = 0.0
    for r, w_ in sorted(zip(rows, ws), key=lambda x: x[0]["dt"]):
        eq += (r["ret"] - COST) * w_ * 100
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"n": len(rows), "win": 100 * win.mean(),
            "unit": 100 * net.sum() / ws.sum(),
            "PF": gw / gl if gl else float("inf"), "cum": eq, "mdd": mdd}


def fmt(s: dict | None, width: int = 22) -> str:
    if not s:
        return f"  {'-':<{width}s} n=0"
    return (f"  {'':<{width}s} n={s['n']:5d} 胜={s['win']:5.1f}% 均={s['mean']:+6.2f}% "
            f"中位={s['med']:+6.2f}% PF={s['PF']:5.2f} 累计={s['cum']:+7.0f}pp "
            f"回撤={s['mdd']:+7.0f}pp MFE={s['mfe']:+5.1f}%")


def fmtw(name: str, s: dict | None, width: int = 22) -> str:
    if not s:
        return f"  {name:<{width}s} n=0"
    return (f"  {name:<{width}s} n={s['n']:5d} 胜={s['win']:5.1f}% 单位均={s['unit']:+6.2f}% "
            f"PF={s['PF']:5.2f} 累计={s['cum']:+7.0f}pp 回撤={s['mdd']:+7.0f}pp")


def show(title: str, items: list[tuple[str, list[dict]]]) -> None:
    print("\n" + "=" * 108)
    print(title)
    print("=" * 108)
    for name, sub in items:
        print(f"{name:<24s}{fmt(stats(sub))}")


def oos_block(title: str, rows: list[dict], mode: str) -> None:
    """在给定子样本上对比 全样本 / G0 / G5。"""
    v = view(rows, mode, no_overlap=True)
    print(f"\n{title}（{mode}，n={len(v)}）")
    for name in ("全样本", "G0_现行", "G5_green2"):
        sub = v if name == "全样本" else gate(v, name)
        s = stats(sub)
        if s:
            print(f"  {name:<10s} n={s['n']:5d} 胜={s['win']:5.1f}% 均={s['mean']:+6.2f}% "
                  f"PF={s['PF']:5.2f} 累计={s['cum']:+7.0f}pp 回撤={s['mdd']:+7.0f}pp "
                  f"MFE={s['mfe']:+5.1f}%")
        else:
            print(f"  {name:<10s} n=0")


def attribution(rows: list[dict], mode: str) -> None:
    v = view(rows, mode, no_overlap=True)
    kept = [r for r in v if r["green2"]]
    drop = [r for r in v if not r["green2"]]
    print("\n" + "=" * 108)
    print(f"③ 归因：green2 保留组 vs 拦截组（{mode}，同币不叠仓）")
    print("=" * 108)
    print(f"  {'组':8s} {'n':>5s} {'占比':>6s} {'止损率':>7s} {'均net':>8s} {'PF':>6s} "
          f"{'累计pp':>9s} {'MFE中位':>8s} {'右尾≥20%':>9s} {'右尾贡献':>9s}")
    for name, sub in (("保留", kept), ("拦截", drop)):
        if not sub:
            continue
        net = np.array([r["ret"] - COST for r in sub])
        gw, gl = net[net > 0].sum(), -net[net < 0].sum()
        right = [r for r in sub if r["mfe"] >= 0.20]
        print(f"  {name:8s} {len(sub):5d} {100*len(sub)/len(v):5.1f}% "
              f"{100*sum(1 for r in sub if r['reason'] == 'sl')/len(sub):6.1f}% "
              f"{100*net.mean():+8.2f} {(gw/gl if gl else 9.99):6.2f} {100*net.sum():+9.0f} "
              f"{100*np.median([r['mfe'] for r in sub]):+8.1f} "
              f"{100*len(right)/len(sub):8.1f}% "
              f"{100*sum(r['ret'] - COST for r in right):+9.0f}")

    print(f"\n  {'MFE 区间':12s} {'保留n':>6s} {'保留均':>8s} {'拦截n':>6s} {'拦截均':>8s} "
          f"{'拦截占比':>9s}")
    for lo, hi in [(0, 0.03), (0.03, 0.05), (0.05, 0.10), (0.10, 0.20),
                   (0.20, 0.50), (0.50, 99)]:
        k = [r for r in kept if lo <= r["mfe"] < hi]
        d = [r for r in drop if lo <= r["mfe"] < hi]
        label = f"{lo*100:.0f}~{hi*100:.0f}%" if hi < 99 else f">{lo*100:.0f}%"
        print(f"  {label:12s} {len(k):6d} "
              f"{(100*np.mean([r['ret'] - COST for r in k]) if k else 0):+8.2f} "
              f"{len(d):6d} "
              f"{(100*np.mean([r['ret'] - COST for r in d]) if d else 0):+8.2f} "
              f"{(100*len(d)/(len(k)+len(d)) if (len(k)+len(d)) else 0):8.1f}%")

    print(f"\n  出场原因分布：保留 {Counter(r['reason'] for r in kept).most_common(4)}")
    print(f"                拦截 {Counter(r['reason'] for r in drop).most_common(4)}")


def sizing(rows: list[dict], mode: str) -> None:
    """A 档 50~75% 亏损池：满仓 / 半仓 / 剔除 三方案对比。"""
    v = view(rows, mode, no_overlap=True)
    print("\n" + "=" * 108)
    print(f"④ A 档 50~75% 仓位倾斜（{mode}，同币不叠仓）")
    print("=" * 108)
    a = [r for r in v if r["sig"] == "A" and not np.isnan(r["chg24"])]
    lo_pool = lambda r: 0.50 <= r["chg24"] < 0.75
    cases = (
        ("A 全满仓", a, None),
        ("A 50~75% 半仓", a, lambda r: 0.5 if lo_pool(r) else 1.0),
        ("A 50~75% 剔除", [r for r in a if not lo_pool(r)], None),
        ("A 50~75% 半仓+green2", [r for r in a if r["green2"]],
         lambda r: 0.5 if lo_pool(r) else 1.0),
        ("A green2 满仓", [r for r in a if r["green2"]], None),
    )
    for name, sub, w in cases:
        print(fmtw(name, wstats(sub, w)))
    b = [r for r in v if r["sig"] == "B"]
    if b:
        print(fmtw("B 全满仓(对照)", wstats(b)))


def account(rows: list[dict], mode: str) -> None:
    """⑤ 全账户（A+B）组合：sl10 单独 vs sl10 + A 档 50~75% 半仓。

    口径：名义本金加权（实盘 A=60U / B=20U，归一化到 A=1.0 即 B=1/3），
    实盘约束（−0.5% 限价 + 同币不叠仓），出场统一 sl10（与实盘一致）。
    用途：确认「深亏左尾(sl10)」与「中段亏损池(A 档半仓)」两个优化叠加后
    PF / 累计 / 回撤均优于任一单独方案。
    """
    v = view(rows, mode, no_overlap=True)
    a_half = lambda r: 0.5 if r["sig"] == "A" and 0.50 <= r["chg24"] < 0.75 else 1.0
    base_w = lambda r: 1.0 if r["sig"] == "A" else 20.0 / 60.0
    print("\n" + "=" * 108)
    print(f"⑤ 全账户（A+B）组合：sl10 单独 vs sl10 + A 档半仓"
          f"（{mode}，名义加权 A=1.0 / B=1/3）")
    print("=" * 108)
    for name, w in (("sl10 单独", base_w),
                    ("sl10 + A 档半仓", lambda r: base_w(r) * a_half(r))):
        print(fmtw(name, wstats(v, w), width=24))
    a = [r for r in v if r["sig"] == "A"]
    b = [r for r in v if r["sig"] == "B"]
    print(fmtw("  ├ A 全满仓", wstats(a), width=24))
    print(fmtw("  ├ A 50~75% 半仓", wstats(a, a_half), width=24))
    print(fmtw("  └ B 全满仓", wstats(b), width=24))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = load_rows() if (args.reuse and OUT_CSV.exists()) else extract(args.limit)

    print("\n" + "=" * 108)
    print("① 口径对比：纯信号池 vs 实盘约束（−0.5% 限价 + 同币不叠仓）")
    print("=" * 108)
    filled = sum(1 for r in rows if r["lim"])
    print(f"  限价成交率 {100*filled/len(rows):.1f}%（{filled:,}/{len(rows):,}）；"
          f"同币不叠仓后再筛掉部分重叠事件")
    for mode, label in (("nl", "纯信号池"), ("lim", "实盘口径")):
        for name in ("全样本", "G0_现行", "G5_green2"):
            for sig in ("A", "B"):
                sub = [r for r in rows if r["sig"] == sig]
                v = view(sub, mode, no_overlap=(mode == "lim"))
                v = v if name == "全样本" else gate(v, name)
                s = stats(v)
                if s:
                    print(f"  {label:6s} {sig} {name:<10s} n={s['n']:5d} 胜={s['win']:5.1f}% "
                          f"均={s['mean']:+6.2f}% PF={s['PF']:5.2f} "
                          f"累计={s['cum']:+7.0f}pp 回撤={s['mdd']:+7.0f}pp")
        print()

    print("=" * 108)
    print("② OOS 稳定性（A 档为主；实盘口径）")
    print("=" * 108)
    a_rows = [r for r in rows if r["sig"] == "A"]
    a_rows.sort(key=lambda r: r["eo"])
    mid = a_rows[len(a_rows) // 2]["eo"]
    oos_block(f"前半段（eo < {mid:%Y-%m-%d}）", [r for r in a_rows if r["eo"] < mid], "lim")
    oos_block(f"后半段（eo ≥ {mid:%Y-%m-%d}）", [r for r in a_rows if r["eo"] >= mid], "lim")
    years = sorted({r["eo"].year for r in a_rows})
    for y in years:
        oos_block(f"分年 {y}", [r for r in a_rows if r["eo"].year == y], "lim")
    syms = sorted({r["sym"] for r in a_rows})
    rng = np.random.default_rng(42)
    rng.shuffle(syms)
    half = set(syms[: len(syms) // 2])
    oos_block("币种随机对拆 A", [r for r in a_rows if r["sym"] in half], "lim")
    oos_block("币种随机对拆 B", [r for r in a_rows if r["sym"] not in half], "lim")

    attribution(rows, "lim")
    attribution(rows, "nl")
    sizing(rows, "lim")
    account(rows, "lim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
