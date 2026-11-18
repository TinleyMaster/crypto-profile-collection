"""出场规则变体回测（本地 1h K 线，口径对齐实盘 execute_gainers_v2 的出场框架）。

现行实盘出场：首小时只吃初始止损（EMA20×(1−2%) 与 −SL 取更紧）→ 第 2 小时起 EMA7 线破
全平 → 浮盈≥5% 保本上移 → 到期收盘平仓。本脚本在同一入场/同一事件集上，逐一验证
「分级止盈 / 动态 EMA / 浮盈时间止盈 / 不创新高 / 到期前离场 / 硬止损宽度」的净效果。

口径：
  - 入场 = 事件 bar 收盘价，t_in = eo+1h，1x，净收益扣 COST（0.3% 往返）
  - 现实成交：跳空穿越止损/止盈线按 bar 开盘价成交，否则按触发价成交
  - 同根 bar 内止损与止盈档同时触发 → 保守按止损先成交
  - 同币不叠仓（前一笔未到期则跳过新事件）

数据源：F:\\代码\\coindata\\coin-binance-swap-candle-csv-1h-2026-11-08（本地，不走 API）

用法：
  python backtest_exit_variants.py                      # 全部变体
  python backtest_exit_variants.py --only base,sl10     # 指定变体
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from backtest_5m_entry import CACHE, DATA_1H, HOUR, COST, csv_sym, load_events, read_klines

# 实盘出场常量（与 execute_gainers_v2 保持一致）
SL_PCT = 0.15          # 硬止损（本脚本的 base 用 15%，即当前实盘值）
EMA_INIT_PERIOD = 20   # 首小时初始止损线 EMA20
EMA_INIT_BUF = 0.02    # ×(1−2%)
EMA_INIT_MIN_GAP = 0.05   # 线距入场不足 5% → 退回固定 SL
EMA_INIT_MIN_BARS = 30
EMA_TRAIL_PERIOD = 7
EMA_TRAIL_MIN_BARS = 30
BE_TRIGGER = 0.05
BE_STOP = 0.004

# partial: {sig: [(浮盈阈值, 减仓比例)]}；ema_dyn: {sig: [(浮盈阈值, EMA周期)]}
# time_tp: {sig: [(窗口小时, 最小浮盈, 减仓比例)]}；nnh: (连续不创新高根数, 最小浮盈)
VARIANTS: dict[str, dict] = {
    "base": dict(),
    # 方向一 分级止盈：A 3%/7%，B 5%/12%
    "pA_3_7": dict(partial={"A": [(0.03, 1 / 3), (0.07, 1 / 3)],
                            "B": [(0.05, 1 / 3), (0.12, 1 / 3)]}),
    "pA_3_8": dict(partial={"A": [(0.03, 1 / 3), (0.08, 1 / 3)],
                            "B": [(0.05, 1 / 3), (0.12, 1 / 3)]}),
    "p_half": dict(partial={"A": [(0.05, 1 / 2)], "B": [(0.08, 1 / 2)]}),
    "p_hi": dict(partial={"A": [(0.10, 1 / 3), (0.25, 1 / 3)],
                          "B": [(0.15, 1 / 3), (0.30, 1 / 3)]}),
    # 方向二 提前保本 / 动态 EMA 收紧
    "be2": dict(be_trigger=0.02),
    "ema_dyn": dict(ema_dyn={"A": [(0.05, 5), (0.15, 3)],
                             "B": [(0.05, 5), (0.15, 3)]}),
    # 方向三 时间止盈 / 不创新高
    "time_tp": dict(time_tp={"A": [(4.0, 0.10, 1 / 3), (6.0, 0.05, 1 / 2)],
                             "B": [(24.0, 0.05, 1 / 3)]}),
    "nnh3": dict(nnh=(3, 0.03)),
    "nnh2": dict(nnh=(2, 0.05)),
    # 方向五 到期前提前离场
    "expiry_pre2": dict(expiry_pre_h=2.0),
    # 按浮盈档位动态收紧（2026-11-11 第二轮）：低档 EMA7 不动，中档（5~15%）收紧，
    # 高档（≥15%）放松回 EMA7/EMA9 —— 目标是只削「中间段最终打止损」的交易，不动右尾
    "dyn_u4": dict(dyn_trail={"mid_thr": 0.05, "high_thr": 0.15, "mid_mode": "dd",
                              "dd": 0.04, "mid_ema": 5, "high_ema": 7}),
    "dyn_u5": dict(dyn_trail={"mid_thr": 0.05, "high_thr": 0.15, "mid_mode": "dd",
                              "dd": 0.05, "mid_ema": 5, "high_ema": 7}),
    "dyn_u_ema5": dict(dyn_trail={"mid_thr": 0.05, "high_thr": 0.15, "mid_mode": "ema",
                                  "dd": 0.05, "mid_ema": 5, "high_ema": 7}),
    "dyn_u9": dict(dyn_trail={"mid_thr": 0.05, "high_thr": 0.15, "mid_mode": "dd",
                              "dd": 0.05, "mid_ema": 5, "high_ema": 9}),
    # 硬止损宽度（负期望是否来自 −15% 段）
    "sl12": dict(sl_pct=0.12),
    "sl10": dict(sl_pct=0.10),
    "sl8": dict(sl_pct=0.08),
    # 组合（用户原方案的推荐组合）
    "combo_c": dict(partial={"A": [(0.03, 1 / 3), (0.07, 1 / 3)],
                             "B": [(0.05, 1 / 3), (0.12, 1 / 3)]},
                    be_trigger=0.02, expiry_pre_h=2.0),
    "combo_a": dict(partial={"A": [(0.03, 1 / 3), (0.07, 1 / 3)],
                             "B": [(0.05, 1 / 3), (0.12, 1 / 3)]},
                    ema_dyn={"A": [(0.05, 5), (0.15, 3)],
                             "B": [(0.05, 5), (0.15, 3)]},
                    time_tp={"A": [(4.0, 0.10, 1 / 3), (6.0, 0.05, 1 / 2)],
                             "B": [(24.0, 0.05, 1 / 3)]},
                    expiry_pre_h=2.0),
}


def ema_last(closes: np.ndarray, period: int) -> float | None:
    if len(closes) < max(period, EMA_TRAIL_MIN_BARS):
        return None
    ema = float(closes[:period].mean())
    alpha = 2.0 / (period + 1)
    for c in closes[period:]:
        ema += alpha * (c - ema)
    return ema


def ema_4h_last(closes_1h: np.ndarray, period: int) -> float | None:
    """把 1h 收盘按 4 根一组取末根，聚合成 4h 收盘后算 EMA（方向四：跨周期均线）。"""
    n4 = len(closes_1h) // 4
    if n4 < max(period, 30):
        return None
    off = len(closes_1h) - n4 * 4
    return ema_last(closes_1h[off + 3::4], period)


def simulate(arr: dict, i_eo: int, entry: float, n_h: int, cfg: dict, sig: str):
    """返回 (gross_ret, reason, hold_h, mfe) ；数据不足返回 None。"""
    t_in = int(arr["t"][i_eo]) + HOUR
    deadline = t_in + n_h * HOUR
    n = len(arr["t"])

    lo = max(0, i_eo - 140)
    sl_wide = cfg.get("sl_pct", SL_PCT)
    cond_sl = cfg.get("cond_sl")
    # 方向一：入场后未拉出安全垫前用更紧止损，armed 后放宽回 sl_wide
    sl_now = cond_sl["tight_sl"] if cond_sl else sl_wide
    stop = entry * (1 - sl_now)
    pre_c = arr["c"][lo:i_eo + 1]
    if len(pre_c) >= EMA_INIT_MIN_BARS:
        e20 = ema_last(pre_c, EMA_INIT_PERIOD)
        if e20:
            line0 = e20 * (1 - EMA_INIT_BUF)
            if line0 <= entry * (1 - EMA_INIT_MIN_GAP):
                stop = max(line0, entry * (1 - sl_now))

    be_trigger = cfg.get("be_trigger", BE_TRIGGER)
    partial = cfg.get("partial", {}).get(sig, [])
    ema_dyn = cfg.get("ema_dyn", {}).get(sig, [])
    time_tp = sorted(cfg.get("time_tp", {}).get(sig, []))
    expiry_pre_h = cfg.get("expiry_pre_h", 0.0)
    nnh = cfg.get("nnh")
    dyn = cfg.get("dyn_trail")
    loss_cut = cfg.get("loss_cut")              # 方向二：浮亏分级减仓
    trail_confirm = cfg.get("trail_confirm")    # 方向三：收盘确认反转
    trail_period = cfg.get("trail_period", EMA_TRAIL_PERIOD)   # 方向四：换周期均线
    trail_period_4h = cfg.get("trail_period_4h")

    remaining, realized = 1.0, 0.0
    exits: list[str] = []
    be_active, be_stop = False, entry * (1 + BE_STOP)
    mfe = peak = 0.0
    since_high = 0
    taken: set = set()
    armed = False       # 方向一：是否已拉出安全垫
    below_cnt = 0       # 方向三：连续收盘破线根数

    i = i_eo + 1
    if i >= n:
        return None
    while i < n and arr["t"][i] < t_in:
        i += 1
    while i < n:
        t, o, h, l, c = (float(arr[k][i]) for k in ("t", "o", "h", "l", "c"))

        def take(px: float, w: float, reason: str) -> None:
            nonlocal remaining, realized
            if w <= 1e-12 or remaining <= 1e-12:
                return
            w = min(w, remaining)
            realized += w * (px / entry - 1)
            remaining -= w
            exits.append(reason)

        if t >= deadline:
            take(o, remaining, "expire")
            break

        # EMA 线（用 t 之前已收盘的 1h，含 eo bar）
        period = trail_period
        for thr, p_ in sorted(ema_dyn):
            if peak / entry - 1 >= thr:
                period = p_
        dd_line = None
        if dyn:
            pk = peak / entry - 1
            if pk >= dyn["high_thr"]:
                period = dyn["high_ema"]
            elif pk >= dyn["mid_thr"]:
                if dyn["mid_mode"] == "dd":
                    dd_line = peak * (1 - dyn["dd"])
                else:
                    period = dyn["mid_ema"]
            else:
                period = EMA_TRAIL_PERIOD
        line = None
        if t >= t_in + HOUR:
            if dd_line is not None:
                line = dd_line
            elif trail_period_4h:
                line = ema_4h_last(arr["c"][max(0, i - 800):i], trail_period_4h)
            else:
                hist = arr["c"][max(0, i - 60):i]
                line = ema_last(hist, period)

        # 方向二：浮亏分级减仓（5% 线先于 10% 硬止损成交）
        if loss_cut and "lcut" not in taken:
            cut_line = entry * (1 - loss_cut["dd"])
            if l <= cut_line:
                take(min(o, cut_line), loss_cut["frac"], "lcut")
                taken.add("lcut")

        if trail_confirm:
            # 方向三：收盘确认反转才出场，不做 intra-bar 破线
            if line is not None:
                below_cnt = below_cnt + 1 if c < line else 0
                slope_ok = True
                if trail_confirm.get("slope"):
                    prev_line = ema_last(arr["c"][max(0, i - 61):i - 1], period)
                    slope_ok = prev_line is not None and line < prev_line
                if below_cnt >= trail_confirm["bars"] and slope_ok:
                    take(c, remaining, "trail2")
                    break
            line_hit = False
        else:
            line_hit = line is not None and (o <= line or l <= line)
        stop_hit = l <= stop
        if line_hit or stop_hit:
            if line_hit and (not stop_hit or line >= stop):
                take(o if o <= line else line, remaining, "trail")
            else:
                reason = "breakeven" if be_active and abs(stop - be_stop) < 1e-12 else "sl"
                take(o if o < stop else stop, remaining, reason)
            break

        for lv, frac in partial:
            if lv not in taken and h / entry - 1 >= lv:
                take(max(o, entry * (1 + lv)), frac, f"tp{round(lv * 100)}")
                taken.add(lv)

        prev_peak = peak
        peak = max(peak, h)
        mfe = max(mfe, h / entry - 1)
        since_high = 0 if h > prev_peak else since_high + 1
        elapsed = (t + HOUR - t_in) / HOUR

        # 方向一：一旦拉出安全垫，止损放宽回 sl_wide（只放宽不收紧）
        if cond_sl and not armed and peak / entry - 1 >= cond_sl["arm_pct"]:
            armed = True
            stop = min(stop, entry * (1 - sl_wide))

        if nnh and since_high >= nnh[0] and c / entry - 1 >= nnh[1]:
            take(c, remaining, "nnh")
            break

        if not be_active and elapsed >= 1.0 and peak / entry - 1 >= be_trigger \
                and c >= peak * 0.995:
            be_active = True
            stop = max(stop, be_stop)

        for k, (win_h, min_pnl, frac) in enumerate(time_tp):
            key = f"time{k}"
            if key in taken or elapsed > win_h:
                continue
            taken.add(key)
            if c / entry - 1 >= min_pnl:
                take(c, frac, f"tpT{int(win_h)}")

        if expiry_pre_h > 0:
            left_h = (deadline - (t + HOUR)) / HOUR
            if 0 <= left_h <= expiry_pre_h and c / entry - 1 > 0:
                take(c, remaining, "expire_pre")
                break

        if remaining <= 1e-12:
            break
        i += 1

    if remaining > 1e-12:
        return None
    return realized, "+".join(exits), (t + HOUR - t_in) / HOUR, mfe


def stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    net = np.array([r["ret"] - COST for r in rows])
    win = net > 0
    gross_win = net[win].sum()
    gross_loss = -net[~win].sum()
    eq = peak = mdd = 0.0
    for r in sorted(rows, key=lambda x: x["dt"]):
        eq += (r["ret"] - COST) * 100
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {
        "n": len(net), "win": 100 * win.mean(), "mean": 100 * net.mean(),
        "med": 100 * np.median(net), "PF": gross_win / gross_loss if gross_loss else float("inf"),
        "p10": 100 * np.percentile(net, 10), "worst": 100 * net.min(),
        "cum": eq, "mdd": mdd,
        "mfe": 100 * np.median([r["mfe"] for r in rows]),
        "give": 100 * np.median([r["mfe"] - (r["ret"] - COST) for r in rows]),
    }


def load_chg24() -> dict[tuple[str, int], float]:
    """(symbol, eo_ts) → chg24，用于入场端分档归因。"""
    out: dict[tuple[str, int], float] = {}
    with CACHE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                eo = datetime.fromisoformat(r["eo"])
                if eo.tzinfo is None:
                    eo = eo.replace(tzinfo=timezone.utc)
                out[(r["symbol"], int(eo.timestamp()))] = float(r["chg24"])
            except (ValueError, TypeError, KeyError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑指定变体（逗号分隔）")
    args = ap.parse_args()
    names = [x.strip() for x in args.only.split(",") if x.strip()] or list(VARIANTS)

    events = load_events()
    chg24_map = load_chg24()
    now = datetime.now(timezone.utc)
    events = [e for e in events
              if e["eo"] + timedelta(hours=e["n_h"] + 2) <= now]
    print(f"[events] {len(events):,} 条（缓存 {CACHE.name}）"
          f" A={sum(e['sig'] == 'A' for e in events):,}"
          f" B={sum(e['sig'] == 'B' for e in events):,}")

    by_sym: dict[str, list] = defaultdict(list)
    for e in events:
        by_sym[e["sym"]].append(e)

    arrs, miss = {}, 0
    for sym in by_sym:
        a = read_klines(DATA_1H / f"{csv_sym(sym)}.csv")
        if a is None:
            miss += len(by_sym[sym])
        else:
            arrs[sym] = a
    print(f"[data] 本地 1h：{len(arrs)} 个币，缺文件跳过事件 {miss:,}")

    cutoff = now - timedelta(days=90)
    rep: dict[str, dict] = {}
    for name in names:
        cfg = VARIANTS[name]
        rows_all: list[dict] = []
        for sig in ("A", "B"):
            last: dict[str, datetime] = {}
            for sym, evs in by_sym.items():
                a = arrs.get(sym)
                if a is None:
                    continue
                for ev in sorted(evs, key=lambda x: x["eo"]):
                    if ev["sig"] != sig:
                        continue
                    t_in = ev["eo"] + timedelta(hours=1)
                    if sym in last and t_in < last[sym]:
                        continue
                    ts = int(ev["eo"].timestamp())
                    i_eo = int(np.searchsorted(a["t"], ts, side="left"))
                    if i_eo >= len(a["t"]) or a["t"][i_eo] != ts:
                        continue
                    out = simulate(a, i_eo, ev["entry"], ev["n_h"], cfg, sig)
                    if out is None:
                        continue
                    ret, reason, hold_h, mfe = out
                    rows_all.append({"ret": ret, "reason": reason, "mfe": mfe,
                                     "sig": sig, "eo": ev["eo"],
                                     "chg24": chg24_map.get((sym, ts), float("nan")),
                                     "dt": t_in + timedelta(hours=hold_h)})
                    last[sym] = t_in + timedelta(hours=hold_h)
        rep[name] = {
            "all": stats(rows_all),
            "A": stats([r for r in rows_all if r["sig"] == "A"]),
            "B": stats([r for r in rows_all if r["sig"] == "B"]),
            "all90": stats([r for r in rows_all if r["eo"] >= cutoff]),
            "A90": stats([r for r in rows_all if r["sig"] == "A" and r["eo"] >= cutoff]),
            "B90": stats([r for r in rows_all if r["sig"] == "B" and r["eo"] >= cutoff]),
            "reasons": Counter(r["reason"] for r in rows_all).most_common(6),
            "rows": rows_all,
        }

    def fmt(s):
        if not s:
            return "n=0"
        return (f"n={s['n']:5d} 胜={s['win']:5.1f}% 均={s['mean']:+6.2f}% "
                f"中位={s['med']:+6.2f}% PF={s['PF']:5.2f} p10={s['p10']:+6.2f}% "
                f"最差={s['worst']:+6.2f}% 累计={s['cum']:+7.0f}pp "
                f"回撤={s['mdd']:+7.0f}pp MFE={s['mfe']:+5.1f}% 回吐={s['give']:+5.1f}%")

    for title, keys in (("全历史", ("all", "A", "B")), ("近90天", ("all90", "A90", "B90"))):
        print("\n" + "=" * 152)
        print(f"📊 {title}（net 扣 {COST*100:.1f}%，现实成交，1h 粒度，1x 固定名义）")
        print("=" * 152)
        for name in names:
            for k in keys:
                print(f"  {name:12s} {k:5s} {fmt(rep[name][k])}")
            print(f"  {'':12s} {'出场':5s} {rep[name]['reasons']}")
            print()

    base = rep.get("base")
    if base:
        rows = base["rows"]
        print("=" * 96)
        print("🔍 归因 1：base 按出场原因分解（net）")
        print("=" * 96)
        by_r: dict[str, list] = defaultdict(list)
        for r in rows:
            by_r[r["reason"]].append(r["ret"] - COST)
        print(f"  {'reason':12s} {'n':>5s} {'占比':>6s} {'均net':>8s} {'累计pp':>9s}")
        for reason, vals in sorted(by_r.items(), key=lambda kv: -len(kv[1])):
            print(f"  {reason:12s} {len(vals):5d} {100*len(vals)/len(rows):5.1f}% "
                  f"{100*np.mean(vals):+8.2f} {100*sum(vals):+9.0f}")

        print("\n" + "=" * 96)
        print("🔍 归因 2：base 按「最大浮盈 MFE」分组（利润来自哪一段）")
        print("=" * 96)
        print(f"  {'MFE 区间':14s} {'n':>5s} {'占比':>6s} {'均net':>8s} {'累计pp':>9s}")
        for lo, hi in [(-1, 0), (0, 0.03), (0.03, 0.05), (0.05, 0.10),
                       (0.10, 0.20), (0.20, 0.50), (0.50, 99)]:
            vals = [r["ret"] - COST for r in rows if lo <= r["mfe"] < hi]
            if not vals:
                continue
            label = f">{lo*100:.0f}%" if hi >= 99 else f"{lo*100:.0f}%~{hi*100:.0f}%"
            print(f"  {label:14s} {len(vals):5d} {100*len(vals)/len(rows):5.1f}% "
                  f"{100*np.mean(vals):+8.2f} {100*sum(vals):+9.0f}")

        print("\n" + "=" * 96)
        print("🔍 归因 3：base 按 chg24 分档（入场端左尾分布，检验「涨幅分层过滤」）")
        print("=" * 96)
        print(f"  {'档位':16s} {'n':>5s} {'占比':>6s} {'止损率':>7s} {'均net':>8s} "
              f"{'PF':>6s} {'累计pp':>9s} {'MFE中位':>8s}")
        for sig, buckets in (
            ("A", [(0.50, 0.75), (0.75, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)]),
            ("B", [(0.30, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 0.50)]),
        ):
            sub_all = [r for r in rows if r["sig"] == sig and not np.isnan(r["chg24"])]
            if not sub_all:
                continue
            for lo, hi in buckets:
                sub = [r for r in sub_all if lo <= r["chg24"] < hi]
                if not sub:
                    continue
                net = np.array([r["ret"] - COST for r in sub])
                gw, gl = net[net > 0].sum(), -net[net < 0].sum()
                sl_rate = 100 * sum(1 for r in sub if r["reason"] == "sl") / len(sub)
                label = (f"{sig} {lo*100:.0f}~{hi*100:.0f}%" if hi < 99
                         else f"{sig} >{lo*100:.0f}%")
                print(f"  {label:16s} {len(sub):5d} {100*len(sub)/len(sub_all):5.1f}% "
                      f"{sl_rate:6.1f}% {100*net.mean():+8.2f} "
                      f"{(gw/gl if gl else float('inf')):6.2f} {100*net.sum():+9.0f} "
                      f"{100*np.median([r['mfe'] for r in sub]):+8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
