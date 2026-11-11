#!/usr/bin/env python3
"""A 档位置类特征单变量回测：距 24h 高点回撤（dd）/ 放量第 N 根（N）。

背景（2026-11-11 第四轮）：A 档已落地「chg24 ∈ [50%,75%) 半仓」与「B 档名义减半 + N 24h」，
两类改动都是风险预算，不提取 alpha。本轮验证两个**位置类**入场特征能否提升右尾捕获：

  ① dd = 1 − 信号现价 / 24h 内 1h high 最大值
     高点取 **1h**（不是 5m）：小币 5m 插针极多，单根 5m 高点无趋势意义；1h 高点代表
     资金共识形成的阶段顶部，与「行情是否已冲高回落」的位置逻辑匹配。
     现价 = 信号时刻的 5m 收盘（= 事件 1h bar 收盘 = nl 入场价；限价口径再 −0.5%）。
     分档 [0,1%) / [1%,3%) / [3%,5%) / [5%,∞)。
  ② N = 放量第几根：单根 1h quote_volume ÷ 前 24 根 1h 均量 ≥ 3 视为放量，
     取「含信号 bar 在内的最近一段连续放量」的起点，N = 信号 bar 序号 − 起点序号 + 1。
     阈值取 3（不是 B 档门槛 5.6）：这里问的是「放量持续了多久」，5.6 会把温和放量段
     全部跳过，导致 N 全挤在 1~2、失去区分度。
     分档 N=1 / N=2 / N=3~4 / N≥5；24h 内无 vr≥3 → N=0（突发爆量）。
  边界：上市不足 24h（1h 序列不足 24 根）→ 单列「新股」组，不并入任何档位。

方法：复用 data/_green2_events.csv 的 per-event 结果（已含 nl_/lim_ 双口径 + MFE + 出场原因），
只补算 1h 特征，不再重跑出场模拟。口径固定为实盘：lim（−0.5% 限价成交）+ 同币不叠仓。
统计重点不是均净，而是**逻辑单调性**：dd 越小右尾应越厚、止损率越低；N 越小均净/右尾应越高。

用法：
  python bin/backtest_a_position.py            # 全量（读 1h CSV，数分钟）
"""
from __future__ import annotations

import io
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_5m_entry import DATA_1H, csv_sym  # noqa: E402
from backtest_exit_variants import stats  # noqa: E402
from backtest_momentum_gate import load_rows  # noqa: E402

DD_BUCKETS = [(0.00, 0.01, "[0,1%) 创新高附近"),
              (0.01, 0.03, "[1,3%)"),
              (0.03, 0.05, "[3,5%)"),
              (0.05, 9.99, "[5%,∞) 深度回落")]
N_BUCKETS = [(0, 0, "N=0 无放量(突发)"),
             (1, 1, "N=1 放量首根"),
             (2, 2, "N=2"),
             (3, 4, "N=3~4"),
             (5, 10**9, "N≥5 放量末期")]
VR_SURGE = 3.0
LOOKBACK = 24          # 1h 根数：dd 高点窗 / vr 均量窗 / N 回溯上限


# --------------------------------------------------------------------------- 1h 数据
def read_1h(sym: str) -> dict | None:
    """读 1h CSV → t/high/close/quote_volume（复用 read_klines 的解码容错）。"""
    path = DATA_1H / f"{csv_sym(sym)}.csv"
    if not path.exists():
        return None
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if lines and not lines[0].startswith("candle_begin_time"):
        lines = lines[1:]
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines)),
                         usecols=["candle_begin_time", "high", "close", "quote_volume"],
                         on_bad_lines="skip")
    except ValueError:
        return None
    df["t"] = pd.to_datetime(df["candle_begin_time"], errors="coerce")
    df = df.dropna(subset=["t"]).sort_values("t")
    if df.empty:
        return None
    return {"t": (df["t"].astype("int64") // 10**9).to_numpy(),
            "h": df["high"].to_numpy(float),
            "c": df["close"].to_numpy(float),
            "q": df["quote_volume"].to_numpy(float)}


def _vr(q: np.ndarray, k: int) -> float | None:
    """第 k 根 1h 的量比 = quote_vol[k] ÷ 前 24 根均量（需 k ≥ 24）。"""
    if k < LOOKBACK or k >= len(q):
        return None
    base = float(q[k - LOOKBACK:k].mean())
    return float(q[k] / base) if base > 0 else None


def features(h1: dict, eo_ts: int) -> dict:
    """事件 1h bar（open_time=eo_ts）的 dd / N；数据不足标 new=True。"""
    t = h1["t"]
    i = int(np.searchsorted(t, eo_ts, side="left"))
    if i >= len(t) or t[i] != eo_ts:
        return {"dd": None, "N": None, "new": False, "ok": False}
    if i < LOOKBACK:
        return {"dd": None, "N": None, "new": True, "ok": True}
    hi = float(h1["h"][i - LOOKBACK + 1:i + 1].max())   # 含事件 bar 的 24 根
    px = float(h1["c"][i])                              # 信号现价 = 事件 bar 收盘
    dd = 1.0 - px / hi if hi > 0 else None

    q = h1["q"]
    j = None                                            # 最近一根 vr≥3 的 bar
    for k in range(i, max(i - LOOKBACK, LOOKBACK) - 1, -1):
        v = _vr(q, k)
        if v is not None and v >= VR_SURGE:
            j = k
            break
    if j is None:
        n = 0
    else:                                               # 回溯到该段连续放量的起点
        k = j
        while k - 1 >= LOOKBACK and i - k < LOOKBACK:
            v = _vr(q, k - 1)
            if v is None or v < VR_SURGE:
                break
            k -= 1
        n = i - k + 1
    return {"dd": dd, "N": n, "new": False, "ok": True}


# --------------------------------------------------------------------------- 构建样本
def build(mode: str = "lim") -> list[dict]:
    """A 档 + 实盘口径（lim 已成交）+ 同币不叠仓；补 1h 特征。"""
    picked = []
    for r in load_rows():
        if r["sig"] != "A":
            continue
        p = r[mode]
        if p is None:
            continue
        picked.append({"sym": r["sym"], "ret": p["ret"], "reason": p["reason"],
                       "mfe": p["mfe"], "entry": p["entry"], "exit": p["exit"],
                       "chg24": r["chg24"],
                       "eo": int(r["eo"].timestamp()),
                       "dt": datetime.fromtimestamp(p["exit"], timezone.utc)})
    out, last = [], {}
    for p in sorted(picked, key=lambda x: x["entry"]):
        k = (p["sym"], "A")
        if k in last and p["entry"] < last[k]:
            continue
        out.append(p)
        last[k] = p["exit"]

    cache: dict[str, dict | None] = {}
    miss = 0
    for p in out:
        if p["sym"] not in cache:
            cache[p["sym"]] = read_1h(p["sym"])
        h1 = cache[p["sym"]]
        f = features(h1, p["eo"]) if h1 else {"dd": None, "N": None, "new": False, "ok": False}
        p.update(f)
        if not f["ok"]:
            miss += 1
    print(f"[data] A 档实盘口径 {len(out):,} 笔；1h 特征不可算 {miss:,}；"
          f"新股(<24根) {sum(1 for p in out if p['new']):,}", flush=True)
    return out


# --------------------------------------------------------------------------- 统计
def gstats(rows: list[dict]) -> dict | None:
    s = stats(rows)
    if not s:
        return None
    s["r20"] = 100 * float(np.mean([r["mfe"] >= 0.20 for r in rows]))
    s["sl"] = 100 * float(np.mean([r["reason"] == "sl" for r in rows]))
    s["tr"] = 100 * float(np.mean([r["reason"] == "trail" for r in rows]))
    return s


def line(name: str, s: dict | None, total: int) -> str:
    if not s:
        return f"  {name:<18s} n=0"
    return (f"  {name:<18s} n={s['n']:5d} ({100*s['n']/total:5.1f}%) 胜={s['win']:5.1f}% "
            f"均={s['mean']:+6.2f}% 中位={s['med']:+6.2f}% PF={s['PF']:5.2f} "
            f"累计={s['cum']:+7.0f}pp 回撤={s['mdd']:+7.0f}pp "
            f"MFE={s['mfe']:+5.1f}% 右尾={s['r20']:4.1f}% 止损={s['sl']:4.1f}% 线破={s['tr']:4.1f}%")


def section(title: str, rows: list[dict], key: str, buckets, total: int) -> None:
    print("\n" + "=" * 150)
    print(title)
    print("=" * 150)
    for lo, hi, label in buckets:
        sub = [r for r in rows if r[key] is not None and lo <= r[key] <= hi]
        print(line(label, gstats(sub), total))


def wstats(rows: list[dict]) -> dict | None:
    """账户口径：按实盘 position_scale 加权（chg24∈[50%,75%) 半仓，其余满仓）。"""
    if not rows:
        return None
    ws = np.array([0.5 if (r["chg24"] == r["chg24"] and 0.50 <= r["chg24"] < 0.75)
                   else 1.0 for r in rows], float)
    net = np.array([r["ret"] for r in rows]) * ws
    gw, gl = net[net > 0].sum(), -net[net < 0].sum()
    eq = peak = mdd = 0.0
    for r, w in sorted(zip(rows, ws), key=lambda x: x[0]["dt"]):
        eq += r["ret"] * w * 100
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"n": len(rows), "PF": gw / gl if gl else float("inf"), "cum": eq, "mdd": mdd}


def wline(name: str, s: dict | None, total: int) -> str:
    if not s:
        return f"  {name:<26s} n=0"
    return (f"  {name:<26s} n={s['n']:5d} ({100*s['n']/total:5.1f}%) "
            f"PF={s['PF']:5.2f} 累计={s['cum']:+7.0f}pp 回撤={s['mdd']:+7.0f}pp")


def oos(rows: list[dict], key: str, lo: float, hi: float, title: str) -> None:
    sub = sorted([r for r in rows if r[key] is not None and lo <= r[key] <= hi],
                 key=lambda x: x["dt"])
    if not sub:
        return
    half = len(sub) // 2
    print("\n" + "=" * 150)
    print(f"{title}  (n={len(sub)})")
    print("=" * 150)
    print(line("前半段", gstats(sub[:half]), len(sub)))
    print(line("后半段", gstats(sub[half:]), len(sub)))
    by_year: dict[int, list[dict]] = defaultdict(list)
    for r in sub:
        by_year[r["dt"].year].append(r)
    for y in sorted(by_year):
        print(line(f"{y} 年", gstats(by_year[y]), len(by_year[y])))


def main() -> None:
    rows = build("lim")
    rows = [r for r in rows if r["ok"]]
    new = [r for r in rows if r["new"]]
    base = [r for r in rows if not r["new"]]
    total = len(base)
    print("\n" + "=" * 150)
    print(f"A 档基准（不含新股，n={total}，lim + 同币不叠仓）")
    print("=" * 150)
    print(line("全体", gstats(base), total))
    print(line("新股(<24根1h)", gstats(new), total))

    section("① 距 24h 高点回撤 dd（1h 高点 / 信号 5m 收盘）", base, "dd", DD_BUCKETS, total)
    section("② 放量第 N 根（1h 量比≥3 连续段起点）", base, "N", N_BUCKETS, total)

    # 组合验证：剔除位置最差的组
    print("\n" + "=" * 150)
    print("③ 组合验证（在 A 档基准上剔除单组，看整体 PF / 累计 / 回撤）")
    print("=" * 150)
    print(line("基准", gstats(base), total))
    for name, cond in (
        ("剔除 dd≥5%", lambda r: r["dd"] is not None and r["dd"] >= 0.05),
        ("剔除 N≥5", lambda r: r["N"] is not None and r["N"] >= 5),
        ("剔除 dd≥5% + N≥5", lambda r: (r["dd"] is not None and r["dd"] >= 0.05)
         or (r["N"] is not None and r["N"] >= 5)),
    ):
        kept = [r for r in base if not cond(r)]
        print(line(name, gstats(kept), total))

    oos(base, "dd", 0.05, 9.99, "④ OOS：dd≥5% 组")
    oos(base, "N", 5, 10**9, "⑤ OOS：N≥5 组")
    oos(base, "dd", 0.0, 0.01, "⑥ OOS：dd<1%（创新高附近）组")

    # 混杂检验：dd 与 chg24 是否同源（若 dd≥5% 只是「50~75% 半仓档」的代理，则不算新特征）
    print("\n" + "=" * 150)
    print("⑦ 混杂检验：dd 与 chg24 交叉（A 档内部）")
    print("=" * 150)
    c = [r for r in base if r["dd"] is not None and r["chg24"] == r["chg24"]]
    print(f"  corr(dd, chg24) = {np.corrcoef([r['dd'] for r in c], [r['chg24'] for r in c])[0, 1]:+.3f}")
    for lo, hi, name in ((0.50, 0.75, "chg24 50~75%"), (0.75, 99.0, "chg24 ≥75%")):
        sub = [r for r in c if lo <= r["chg24"] < hi]
        print(line(f"{name} 全体", gstats(sub), len(sub)))
        print(line("  └ dd<5%", gstats([r for r in sub if r["dd"] < 0.05]), len(sub)))
        print(line("  └ dd≥5%", gstats([r for r in sub if r["dd"] >= 0.05]), len(sub)))

    # ⑧ 条件过滤：只剔「chg24 50~75% 且 dd≥5%」，保留 ≥75% 档（该档 dd≥5% 为正）
    print("\n" + "=" * 150)
    print("⑧ 条件过滤落地（仅剔 chg24∈[50%,75%) 且 dd≥5%；账户口径含 A 档半仓权重）")
    print("=" * 150)

    def bad(r: dict) -> bool:
        return (r["dd"] is not None and r["dd"] >= 0.05
                and r["chg24"] == r["chg24"] and 0.50 <= r["chg24"] < 0.75)

    print(wline("现行（A 半仓）", wstats(base), total))
    print(wline("条件剔除后", wstats([r for r in base if not bad(r)]), total))
    print(wline("对照：全局剔 dd≥5%", wstats([r for r in base
                                            if not (r["dd"] is not None and r["dd"] >= 0.05)]), total))
    kept = sorted([r for r in base if not bad(r)], key=lambda x: x["dt"])
    half = len(kept) // 2
    print("\n  —— 条件剔除后的 OOS（保留池本身是否稳定为正）——")
    print(line("前半段", gstats(kept[:half]), len(kept)))
    print(line("后半段", gstats(kept[half:]), len(kept)))
    by_year: dict[int, list[dict]] = defaultdict(list)
    for r in kept:
        by_year[r["dt"].year].append(r)
    for y in sorted(by_year):
        print(line(f"{y} 年", gstats(by_year[y]), len(by_year[y])))

    print("\n  —— 被剔除组本身（chg24∈[50%,75%) 且 dd≥5%）的分年表现 ——")
    cut = sorted([r for r in base if bad(r)], key=lambda x: x["dt"])
    by_year = defaultdict(list)
    for r in cut:
        by_year[r["dt"].year].append(r)
    print(line("合计", gstats(cut), len(cut)))
    arr = np.array([r["ret"] for r in cut], float)
    print(f"    t = {arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))):+.2f}"
          f"（SE={100*arr.std(ddof=1)/np.sqrt(len(arr)):.2f}pp，均值 {100*arr.mean():+.2f}%）")
    for y in sorted(by_year):
        print(line(f"{y} 年", gstats(by_year[y]), len(by_year[y])))

    print("\n  —— dd 阈值敏感性（chg24∈[50%,75%) 内剔除 dd≥thr；阈值附近是否平滑）——")
    for thr in (0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
        sub = [r for r in base if r["dd"] is not None and r["dd"] >= thr
               and r["chg24"] == r["chg24"] and 0.50 <= r["chg24"] < 0.75]
        kept2 = [r for r in base if not (r["dd"] is not None and r["dd"] >= thr
                                        and r["chg24"] == r["chg24"] and 0.50 <= r["chg24"] < 0.75)]
        s, w = gstats(sub), wstats(kept2)
        print(f"  thr={thr*100:4.0f}%  剔除组 n={s['n']:4d} 均={s['mean']:+6.2f}% PF={s['PF']:5.2f}"
              f"  → 账户 PF={w['PF']:5.2f} 累计={w['cum']:+7.0f}pp 回撤={w['mdd']:+7.0f}pp")


if __name__ == "__main__":
    main()
