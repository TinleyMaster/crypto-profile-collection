#!/usr/bin/env python3
"""5m 入场时机回测：立即入场 vs 回撤限价 vs 确认入场（本地 1h/5m K 线 CSV）。

问题（2026-11-11 用户提出）：从 5 分钟周期寻找入场，对 v2 涨幅榜策略有没有帮助？

事件源：data/trade_params_events.csv（与 backtest_trade_params.py 同一事件缓存）
        事件 = 1h 放量大阳 bar；entry = 事件 bar 收盘价；eo = 事件 bar 开盘时刻。
K 线源：F:\\代码\\coindata\\coin-binance-swap-candle-{1h,5m}-csv-2026-11-08（本地，不走 API）

信号集（对齐实盘白名单，均做多）：
  A = SHORT_LONG：chg24 ≥ 50%，N=24h，TR 3% / SL 15%
  B = MID_LONG  ：30% ≤ chg24 < 50% 且 vr ≥ 5.6，N=168h，TR 3% / SL 15%

入场变体（信号时刻 sig = eo + 1h，即事件 bar 收盘）：
  V0   基准：sig 立即按事件收盘价入场
  V1   sig 起 1h 内回撤 0.5% 限价（entry×0.995）；未成交 → 窗口末市价补入
  V1s  同 V1，未成交 → 放弃该事件
  V2   sig 起 4h 内回撤 1.0% 限价（entry×0.99）；未成交 → 窗口末市价补入
  V2s  同 V2，未成交 → 放弃该事件
  V3   30min 确认：sig 后 30min 价格仍 ≥ entry 才按 30min 价入场，否则放弃

出场（全变体一致，对齐实盘现行参数）：
  TRAIL 3%（入场后滚动峰值回撤，同 bar 保守先判止损）+ SL 15% + 到期收盘平仓。
  到期锚定入场时刻：expiry = 入场 + (N−1)h（V0 与既有回测 h_off=N 完全一致）。
  跳空：触发 bar 开盘已穿越停价 → 按开盘成交（realistic 口径）。
  入场后首小时用 5m bar 判定（入场在 bar 中途也能立刻止损），之后用 1h bar。
  成本 0.3%/笔（与 published 口径一致），已从净收益扣除。

统计：净收益 mean/med/win/pf/worst、相对 V0 的配对差（同事件）均值 + t 值、
      限价成交率/放弃率/入场价差（bp）。
用法：python bin/backtest_5m_entry.py [--limit 5000]
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
CACHE = DATA_DIR / "trade_params_events.csv"
OUT_CSV = DATA_DIR / "backtest_5m_entry.csv"

DATA_1H = Path(r"F:\代码\coindata\coin-binance-swap-candle-csv-1h-2026-11-08")
DATA_5M = Path(r"F:\代码\coindata\coin-binance-swap-candle-5m-csv-2026-11-08")

COST = 0.003          # 0.3%/笔（round trip），与 published 一致
TR = 0.03
SL = 0.15
HOUR = 3600
BAR5 = 300

SIGS = {
    "A": {"label": "SHORT_LONG chg24≥50%", "n_h": 24,
          "cond": lambda c, v: c >= 0.50},
    "B": {"label": "MID_LONG 30~50 & vr≥5.6", "n_h": 168,
          "cond": lambda c, v: 0.30 <= c < 0.50 and (v or 0) >= 5.6},
}
VARIANTS = ["V0", "V1", "V1s", "V2", "V2s", "V3"]


def csv_sym(s: str) -> str:
    """INITUSDT → INIT-USDT（本地 CSV 命名）。"""
    base = s[:-4] if s.endswith("USDT") else s
    return f"{base}-USDT"


def read_klines(path: Path) -> dict | None:
    if not path.exists():
        return None
    # 部分 1h 文件在表头前有一行 GBK 水印：整文件解码后若首行不是表头则丢弃。
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    if text.startswith("\ufeff"):       # UTF-8 BOM 会干扰首行表头判断
        text = text[1:]
    lines = text.splitlines()
    if lines and not lines[0].startswith("candle_begin_time"):
        lines = lines[1:]
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines)),
                         usecols=["candle_begin_time", "open", "high", "low", "close"],
                         on_bad_lines="skip")
    except ValueError:
        return None
    df["t"] = pd.to_datetime(df["candle_begin_time"], errors="coerce")
    df = df.dropna(subset=["t"]).sort_values("t")
    if df.empty:
        return None
    return {
        "t": (df["t"].astype("int64") // 10**9).to_numpy(),
        "o": df["open"].to_numpy(float),
        "h": df["high"].to_numpy(float),
        "l": df["low"].to_numpy(float),
        "c": df["close"].to_numpy(float),
    }


def load_events() -> list[dict]:
    out = []
    with CACHE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                chg24 = float(r["chg24"])
                vr = float(r["vr"]) if r.get("vr") else 0.0
                entry = float(r["entry"])
            except (ValueError, TypeError):
                continue
            eo = datetime.fromisoformat(r["eo"])
            if eo.tzinfo is None:
                eo = eo.replace(tzinfo=timezone.utc)
            for sig, spec in SIGS.items():
                if spec["cond"](chg24, vr):
                    out.append({"sig": sig, "sym": r["symbol"], "eo": eo,
                                "entry": entry, "n_h": spec["n_h"]})
                    break   # A/B 互斥，命中即停
    return out


def slice_time(arr: dict, lo: int, hi: int) -> dict:
    """[lo, hi) 时间切片。"""
    i = int(np.searchsorted(arr["t"], lo, side="left"))
    j = int(np.searchsorted(arr["t"], hi, side="left"))
    return {k: arr[k][i:j] for k in ("t", "o", "h", "l", "c")}


def plan_entry(variant: str, entry: float, sig: int, f5: dict):
    """按 5m 数据规划入场 → (E, t, kind) 或 None（放弃）。"""
    if variant == "V0":
        return entry, sig, "entry"
    if variant in ("V1", "V1s"):
        w = slice_time(f5, sig, sig + HOUR)
        disc = entry * (1 - 0.005)
        hit = np.nonzero(w["l"] <= disc)[0]
        if len(hit):
            return disc, int(w["t"][hit[0]]), "fill"
        if variant == "V1s" or len(w["t"]) == 0:
            return None
        return float(w["c"][-1]), sig + HOUR, "fallback"
    if variant in ("V2", "V2s"):
        w = slice_time(f5, sig, sig + 4 * HOUR)
        disc = entry * (1 - 0.01)
        hit = np.nonzero(w["l"] <= disc)[0]
        if len(hit):
            return disc, int(w["t"][hit[0]]), "fill"
        if variant == "V2s" or len(w["t"]) == 0:
            return None
        return float(w["c"][-1]), sig + 4 * HOUR, "fallback"
    if variant == "V3":
        w = slice_time(f5, sig, sig + HOUR)
        if len(w["t"]) < 6 or w["t"][5] != sig + 5 * BAR5:   # 需完整 30min
            return None
        c30 = float(w["c"][5])
        if c30 >= entry:
            return c30, sig + 6 * BAR5, "entry"
        return None
    raise ValueError(variant)


def simulate(E: float, t: int, n_h: int, f5: dict, h1: dict) -> float | None:
    """多单净收益（扣 COST）。首小时 5m 精确判定，之后 1h；到期 = 入场+(N−1)h。"""
    expiry_t = t + (n_h - 1) * HOUR
    cut = t if t % HOUR == 0 else (t // HOUR + 1) * HOUR   # 5m/1h 分界
    pre = slice_time(f5, t, cut)
    i = int(np.searchsorted(h1["t"], cut, side="left"))
    j = int(np.searchsorted(h1["t"], expiry_t, side="left"))
    if j >= len(h1["t"]):        # 数据没到到期线
        return None
    segs = [(pre, False)]
    suf = {k: h1[k][i:j + 1] for k in ("t", "o", "h", "l", "c")}  # 含到期 bar

    sl_lvl = E * (1 - SL)
    peak = None

    def _run(seg: dict, is_suffix: bool) -> float | None:
        nonlocal peak
        for k in range(len(seg["t"])):
            o, h, l, c = seg["o"][k], seg["h"][k], seg["l"][k], seg["c"][k]
            expiry_bar = is_suffix and seg["t"][k] >= expiry_t
            if o <= sl_lvl:                     # 跳空穿越止损 → 开盘成交
                return o / E - 1 - COST
            if l <= sl_lvl:
                return sl_lvl / E - 1 - COST
            if peak is not None:
                ts = peak * (1 - TR)
                if o <= ts:
                    return o / E - 1 - COST
                if l <= ts:
                    return ts / E - 1 - COST
            peak = h if peak is None else max(peak, h)
            if expiry_bar:
                return c / E - 1 - COST
        return None

    r = _run(pre, False)
    if r is not None:
        return r
    return _run(suf, True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="限制事件数（调试）")
    args = ap.parse_args()

    events = load_events()
    print(f"[events] A+B 共 {len(events):,} 条（缓存 {CACHE.name}）")
    if args.limit:
        events = events[: args.limit]

    by_sym: dict[str, list] = defaultdict(list)
    for e in events:
        by_sym[e["sym"]].append(e)

    # 事件键 → 变体净收益 / kind / 入场价差
    recs: dict[tuple, dict] = {}
    miss_file = 0
    sanity_n = sanity_bad = 0
    loaded = 0

    for sym, evs in by_sym.items():
        f = csv_sym(sym)
        h1 = read_klines(DATA_1H / f"{f}.csv")
        f5 = read_klines(DATA_5M / f"{f}.csv")
        if h1 is None or f5 is None:
            miss_file += len(evs)
            continue
        loaded += 1
        for ev in evs:
            eo_ts = int(ev["eo"].timestamp())
            sig = eo_ts + HOUR
            entry = ev["entry"]
            # ── 口径核对：事件 bar（open_time=eo）收盘价 vs 缓存 entry ──
            k = int(np.searchsorted(h1["t"], eo_ts, side="left"))
            if k < len(h1["t"]) and h1["t"][k] == eo_ts and sanity_n < 500:
                sanity_n += 1
                if abs(h1["c"][k] / entry - 1) > 0.002:
                    sanity_bad += 1

            key = (sym, eo_ts, ev["sig"])
            rec = {"entry": entry, "n_h": ev["n_h"], "ret": {}, "kind": {}, "dt": {}}
            for v in VARIANTS:
                plan = plan_entry(v, entry, sig, f5)
                if plan is None:
                    rec["ret"][v] = None
                    rec["kind"][v] = "skip"
                    continue
                E, t, kind = plan
                r = simulate(E, t, ev["n_h"], f5, h1)
                rec["ret"][v] = r
                rec["kind"][v] = kind if r is not None else "nodata"
                rec["dt"][v] = (E / entry - 1) * 1e4
            recs[key] = rec

    print(f"[data] 读入 {loaded} 个币；缺文件跳过事件 {miss_file:,}；"
          f"口径核对 {sanity_n} 条，偏差>0.2% 的 {sanity_bad} 条"
          f"{'  ⚠ 时区/口径可能不一致！' if sanity_bad > sanity_n * 0.05 else ' ✓'}")

    # ── 汇总 ──
    rows_out = []
    for sig, spec in SIGS.items():
        keys = [k for k in recs if k[2] == sig]
        print(f"\n== {sig} {spec['label']}  (事件 {len(keys):,}) ==")
        print(f"{'变体':>4} {'n':>6} {'放弃%':>6} {'限价成交%':>8} {'补入%':>6} "
              f"{'入场Δbp中位':>10} {'均值%':>7} {'中位%':>7} {'胜%':>6} "
              f"{'PF':>6} {'最差%':>7} {'配对Δbp':>8} {'t值':>7}")
        v0_map = {k: recs[k]["ret"]["V0"] for k in keys}
        for v in VARIANTS:
            vals = [(k, recs[k]["ret"][v]) for k in keys if recs[k]["ret"][v] is not None]
            if not vals:
                continue
            rs = np.array([r for _, r in vals])
            n_all = len(keys)
            kinds = [recs[k]["kind"][v] for k, _ in vals]
            skip_pct = 100 * (1 - len(vals) / n_all)
            fill_pct = 100 * kinds.count("fill") / n_all
            fb_pct = 100 * kinds.count("fallback") / n_all
            dts = np.array([recs[k]["dt"][v] for k, _ in vals])
            # 配对差（相对 V0，仅双方都有值的事件）
            pair = [(recs[k]["ret"][v] - v0_map[k]) * 1e4
                    for k, _ in vals if v0_map[k] is not None]
            if v == "V0" or not pair:
                dm, tv = 0.0, float("nan")
            else:
                p = np.array(pair)
                dm = float(p.mean())
                tv = float(p.mean() / (p.std(ddof=1) / np.sqrt(len(p)))) if len(p) > 2 else float("nan")
            win = float((rs > 0).mean())
            pf = float((rs[rs > 0].sum() + 1e-12) / (abs(rs[rs <= 0].sum()) + 1e-12))
            print(f"{v:>4} {len(vals):>6,} {skip_pct:>6.1f} {fill_pct:>8.1f} {fb_pct:>6.1f} "
                  f"{np.median(dts):>10.1f} {rs.mean()*100:>7.2f} {np.median(rs)*100:>7.2f} "
                  f"{win*100:>6.1f} {pf:>6.2f} {rs.min()*100:>7.1f} "
                  f"{dm:>8.1f} {tv:>7.1f}")
            rows_out.append([sig, spec["label"], v, len(vals), round(skip_pct, 2),
                             round(fill_pct, 2), round(fb_pct, 2),
                             round(float(np.median(dts)), 1),
                             round(float(rs.mean()), 5), round(float(np.median(rs)), 5),
                             round(win, 4), round(pf, 4), round(float(rs.min()), 4),
                             round(dm, 1), round(tv, 2) if tv == tv else ""])

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["signal", "label", "variant", "n", "skip_pct", "fill_pct",
                    "fallback_pct", "entry_delta_bp_med", "mean", "med", "win", "pf",
                    "worst", "paired_delta_bp_vs_V0", "t_vs_V0"])
        w.writerows(rows_out)
    print(f"\n结果已存 {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
