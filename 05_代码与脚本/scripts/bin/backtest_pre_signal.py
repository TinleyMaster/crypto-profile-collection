#!/usr/bin/env python3
"""前置试错信号（梯度观察池）回测：提前入场到底能不能赚钱、能不能改善入场点位。

背景（2026-11-12 第五轮，用户提案「从发现信号端优化」第一优先级）
  现状：扫描器只在 chg24≥30%（B 档）或 ≥50%（A 档）才出信号，等于只能吃到行情后半段。
  提案：一级观察池 chg24≥15% 只监控；池内满足「1h 量比≥3 且突破近 4h 高点」→ 前置试错信号
        （名义 5U、1x = A 档 60U 的 1/12）；后续涨到 30%/50% 再升级补仓。

两条硬底线（用户明确要求，本脚本严格遵守）
  ① 出场规则完全不动：EMA7 线破 + 硬止损 10% + 到期收盘（与 A/B 档同一 simulate/CFG）
  ② 信号越前置仓位越低：前置信号按 5U 名义计算，不提前止盈，完整保留右尾

本脚本回答四个问题
  Q1 前置信号独立期望：胜率 / PF / 左右尾 / 右尾保留率（MFE≥20% 占比）vs 现 A/B 档
  Q2 配对改善：同一波行情里，前置入场价相对 A/B 入场价便宜多少 bp；前置腿单独盈亏如何
  Q3 升级率：前置信号之后 24h 内涨到 30% / 50% 的比例（观察池转化率）
  Q4 账户口径：A+B 现状 vs 加 5U 前置腿（两种闸门：现行同币不叠仓 / 允许升级叠仓），
     以及前置腿时间加权仓位是否 ≤ A+B 的 30%

参数敏感性：vr 2/3/4 × 突破窗口 2/4/6h × chg24 门槛 10/15/20%（共 27 组，均含 24h 冷却）

数据源：F:\\代码\\coindata\\coin-binance-swap-candle-csv-1h-2026-11-08（本地 1h，不走 API）
        data/trade_params_events.csv（A/B 事件缓存，与实盘扫描口径一致）
入场口径：nl = 信号 bar 收盘立即入场；lim = −0.5% 限价、60min 未成交放弃（用 1h bar 近似，
          与 5m 口径的差异在报告里用已落盘 _green2_events.csv 交叉核对）

用法：
  python bin/backtest_pre_signal.py              # 全量（约 3~6 分钟）
  python bin/backtest_pre_signal.py --limit 80   # 小样本自测
  python bin/backtest_pre_signal.py --no-grid    # 跳过 27 组敏感性
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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_5m_entry import CACHE, COST, DATA_1H, HOUR, csv_sym  # noqa: E402
from backtest_exit_variants import simulate, stats  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

DATA_DIR = SCRIPT_DIR.parent / "data"
OUT_CSV = DATA_DIR / "_pre_signal_events.csv"
CROSS_CSV = DATA_DIR / "_green2_events.csv"     # A/B 5m 限价口径，交叉核对用

CFG = {"sl_pct": 0.10}          # 实盘现行出场：SL 10% + EMA7 线破 + 到期
N_H = 24                        # 三组统一 24h 到期（A 档现值；B 档已改 24h）
LIMIT_DISCOUNT = 0.005          # 实盘限价入场：信号价 −0.5%
LIMIT_WAIT = HOUR               # 挂单 60min 未成交放弃
COOL = 24 * HOUR                # 同币冷却（= 同币不叠仓在信号侧的等价物）
LOOKBACK = 24                   # chg24 回看 24h（容忍缺 bar 上限 36h）
VR_WIN = 20                     # 1h 量比分母：前 20 根均量

# 默认参数（用户提案值）
DEF = dict(chg_lo=0.15, chg_hi=0.30, vr=3.0, w=4)

# 敏感性网格（chg_hi 固定 30%：前置信号必须发生在 B 档之前，否则不叫「前置」）
GRID_VR = [2.0, 3.0, 4.0]
GRID_W = [2, 4, 6]
GRID_CHG = [0.10, 0.15, 0.20]

# 名义本金（1x，与 execute_gainers_v2 对齐）
NOTIONAL = {"A": 60.0, "A_HALF": 30.0, "B": 10.0, "PRE": 5.0}
A_HALF_LO, A_HALF_HI = 0.50, 0.75
ACCOUNT_U = 100.0               # 实验账户


# --------------------------------------------------------------------------- 数据
def read_1h(path: Path) -> dict | None:
    """读 1h CSV → t/o/h/l/c/q（容错：GBK 水印行 / BOM / 编码）。"""
    if not path.exists():
        return None
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("latin-1")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if lines and not lines[0].startswith("candle_begin_time"):
        lines = lines[1:]
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines)),
                         usecols=["candle_begin_time", "open", "high", "low",
                                  "close", "quote_volume"],
                         on_bad_lines="skip")
    except ValueError:
        return None
    df["t"] = pd.to_datetime(df["candle_begin_time"], errors="coerce")
    df = df.dropna(subset=["t"]).sort_values("t")
    df = df[df["close"] > 0]
    if df.empty:
        return None
    return {"t": (df["t"].astype("int64") // 10**9).to_numpy(),
            "o": df["open"].to_numpy(float),
            "h": df["high"].to_numpy(float),
            "l": df["low"].to_numpy(float),
            "c": df["close"].to_numpy(float),
            "q": df["quote_volume"].to_numpy(float)}


def load_ab() -> dict[str, list[dict]]:
    """A/B 事件（与实盘扫描器同一缓存、同一档位口径），按币分组。"""
    out: dict[str, list[dict]] = defaultdict(list)
    with CACHE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                chg24 = float(r["chg24"])
                vr = float(r["vr"]) if r.get("vr") else 0.0
                entry = float(r["entry"])
                eo = datetime.fromisoformat(r["eo"])
            except (ValueError, TypeError, KeyError):
                continue
            if eo.tzinfo is None:
                eo = eo.replace(tzinfo=timezone.utc)
            if chg24 >= 0.50:
                sig = "A"
            elif 0.30 <= chg24 < 0.50 and vr >= 5.6:
                sig = "B"
            else:
                continue
            out[r["symbol"]].append({"sym": r["symbol"], "sig": sig,
                                     "eo": int(eo.timestamp()), "entry": entry,
                                     "chg24": chg24, "vr": vr})
    for v in out.values():
        v.sort(key=lambda x: x["eo"])
    return out


def chg24_series(t: np.ndarray, c: np.ndarray) -> np.ndarray:
    """c[k] ÷ 24h 前最近一根收盘 − 1（回看上限 36h，对齐扫描器 SQL）。"""
    j = np.searchsorted(t, t - 86400, side="right") - 1
    jj = np.maximum(j, 0)
    out = np.full(len(t), np.nan)
    ok = (j >= 0) & ((t - t[jj]) <= 36 * 3600)
    out[ok] = c[ok] / c[jj[ok]] - 1
    return out


def vr_series(q: np.ndarray) -> np.ndarray:
    """1h 量比 = quote_vol[k] ÷ 前 20 根均量。"""
    n = len(q)
    out = np.full(n, np.nan)
    if n <= VR_WIN:
        return out
    cs = np.concatenate(([0.0], np.cumsum(q)))
    avg = (cs[VR_WIN:n] - cs[:n - VR_WIN]) / VR_WIN      # k = VR_WIN .. n-1
    with np.errstate(divide="ignore", invalid="ignore"):
        out[VR_WIN:] = np.where(avg > 0, q[VR_WIN:] / avg, np.nan)
    return out


def hi_prev(h: np.ndarray, w: int) -> np.ndarray:
    """hi[k] = max(high[k−w : k])（不含当前 bar，前置 bar 为 −inf）。"""
    n = len(h)
    out = np.full(n, -np.inf)
    if n > w:
        sw = np.lib.stride_tricks.sliding_window_view(h, w)
        out[w:] = sw[:n - w].max(axis=1)
    return out


def pre_mask(chg, vr, hi, c, chg_lo, chg_hi, vr_min, w) -> np.ndarray:
    m = (chg >= chg_lo) & (chg < chg_hi) & (vr >= vr_min) & (c > hi)
    m &= np.isfinite(vr) & np.isfinite(chg)
    m[:max(VR_WIN, w)] = False
    return m


def dedupe(idx: np.ndarray, t: np.ndarray, cool: int) -> list[int]:
    out, last = [], None
    for k in idx:
        if last is None or t[k] - last >= cool:
            out.append(int(k))
            last = t[k]
    return out


def upgrade_flags(chg: np.ndarray, t: np.ndarray, k: int, hours: int = 24):
    """前置信号后 24h 内 chg24 是否摸到 30% / 50%（升级率）。"""
    j = int(np.searchsorted(t, t[k] + hours * 3600, side="right"))
    seg = chg[k + 1:j]
    if len(seg) == 0 or not np.isfinite(seg).any():
        return False, False
    mx = float(np.nanmax(seg))
    return mx >= 0.30, mx >= 0.50


# --------------------------------------------------------------------------- 统计
def rich(rows: list[dict]) -> dict | None:
    s = stats(rows)
    if not s:
        return None
    net = np.array([r["ret"] - COST for r in rows])
    s["r20"] = 100 * float(np.mean([r["mfe"] >= 0.20 for r in rows]))
    s["sl"] = 100 * float(np.mean([r["reason"] == "sl" for r in rows]))
    s["tr"] = 100 * float(np.mean([r["reason"] == "trail" for r in rows]))
    s["loss_mean"] = 100 * float(net[net < 0].mean()) if (net < 0).any() else 0.0
    return s


def line(name: str, s: dict | None, width: int = 16) -> str:
    if not s:
        return f"  {name:<{width}s} n=0"
    return (f"  {name:<{width}s} n={s['n']:5d} 胜={s['win']:5.1f}% "
            f"均={s['mean']:+6.2f}% 中位={s['med']:+6.2f}% PF={s['PF']:5.2f} "
            f"累计={s['cum']:+7.0f}pp 回撤={s['mdd']:+6.0f}pp "
            f"MFE={s['mfe']:+5.1f}% 右尾={s['r20']:4.1f}% "
            f"止损={s['sl']:4.1f}% 线破={s['tr']:4.1f}% "
            f"p10={s['p10']:+6.1f}% 最差={s['worst']:+6.1f}%")


def pct(xs, q):
    return float(np.percentile(xs, q)) if len(xs) else float("nan")


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 个币（自测）")
    ap.add_argument("--no-grid", action="store_true", help="跳过 27 组敏感性")
    args = ap.parse_args()

    ab = load_ab()
    files = sorted(DATA_1H.glob("*.csv"))
    if args.limit:
        files = files[:args.limit]
    print(f"[data] 1h 文件 {len(files):,} 个；A/B 事件币 {len(ab):,} 个；"
          f"出场 CFG={CFG}，N={N_H}h，冷却 {COOL // HOUR}h", flush=True)

    combos = [(v, w, g) for v in GRID_VR for w in GRID_W for g in GRID_CHG]
    if args.no_grid:
        combos = [(DEF["vr"], DEF["w"], DEF["chg_lo"])]
    grid_rows: dict[tuple, list[dict]] = {c: [] for c in combos}

    pre_rows: list[dict] = []        # 默认参数、nl 口径（用于配对与升级率）
    pre_lim: dict[tuple, dict] = {}  # (sym, t_entry) → lim 结果
    up_rows: list[dict] = []         # 升级路径：前置 5U → 补仓至 B/A 标准仓位（lim 首腿）
    ab_rows: list[dict] = []         # A/B 重算（nl + lim1h），用于同口径对照
    miss = 0

    for nf, path in enumerate(files, 1):
        sym = path.stem.replace("-", "")      # BTC-USDT → BTCUSDT
        arr = read_1h(path)
        if arr is None:
            miss += 1
            continue
        t, c, h = arr["t"], arr["c"], arr["h"]
        if len(t) < LOOKBACK + 2:
            continue
        chg = chg24_series(t, c)
        vr = vr_series(arr["q"])
        hi = {w: hi_prev(h, w) for w in GRID_W}

        # ── A/B 档：重算 nl + lim1h（同口径，便于与前置腿横向比较） ──
        for ev in ab.get(sym, []):
            k = int(np.searchsorted(t, ev["eo"], side="left"))
            if k >= len(t) or t[k] != ev["eo"]:
                continue
            nl = simulate(arr, k, ev["entry"], N_H, CFG, ev["sig"])
            lim = None
            filled = k + 1 < len(t) and arr["l"][k + 1] <= ev["entry"] * (1 - LIMIT_DISCOUNT)
            if filled:
                lim = simulate(arr, k, ev["entry"] * (1 - LIMIT_DISCOUNT),
                               N_H, CFG, ev["sig"])
            for mode, out in (("nl", nl), ("lim", lim)):
                if out is None:
                    continue
                ret, reason, hold, mfe = out
                ab_rows.append({"sym": sym, "sig": ev["sig"], "chg24": ev["chg24"],
                                "mode": mode, "ret": ret, "reason": reason,
                                "mfe": mfe, "t_in": ev["eo"] + HOUR, "px": ev["entry"],
                                "dt": datetime.fromtimestamp(ev["eo"] + HOUR + hold * HOUR,
                                                             timezone.utc),
                                "t_open": ev["eo"] + HOUR,
                                "t_close": ev["eo"] + HOUR + hold * HOUR})

        # ── 前置信号：各参数组合的候选 bar（并集只算一次出场） ──
        cand: dict[tuple, list[int]] = {}
        for combo in combos:
            v, w, g = combo
            m = pre_mask(chg, vr, hi[w], c, g, DEF["chg_hi"], v, w)
            cand[combo] = dedupe(np.nonzero(m)[0], t, COOL)
        uniq = sorted({k for ks in cand.values() for k in ks})
        sim: dict[int, tuple | None] = {}
        for k in uniq:
            sim[k] = simulate(arr, k, float(c[k]), N_H, CFG, "PRE")
        lim_sim: dict[int, tuple | None] = {}
        for k in cand.get((DEF["vr"], DEF["w"], DEF["chg_lo"]), []):
            lim_px = float(c[k]) * (1 - LIMIT_DISCOUNT)
            if k + 1 < len(t) and arr["l"][k + 1] <= lim_px:
                lim_sim[k] = simulate(arr, k, lim_px, N_H, CFG, "PRE")

        for combo, ks in cand.items():
            for k in ks:
                out = sim.get(k)
                if out is None:
                    continue
                ret, reason, hold, mfe = out
                u30, u50 = upgrade_flags(chg, t, k)
                row = {"sym": sym, "t_in": int(t[k]) + HOUR, "chg24": float(chg[k]),
                       "vr": float(vr[k]), "ret": ret, "reason": reason,
                       "mfe": mfe, "up30": u30, "up50": u50,
                       "px": float(c[k]),
                       "dt": datetime.fromtimestamp(int(t[k]) + HOUR + hold * HOUR,
                                                    timezone.utc),
                       "t_open": int(t[k]) + HOUR,
                       "t_close": int(t[k]) + HOUR + hold * HOUR}
                grid_rows[combo].append(row)
                if combo == (DEF["vr"], DEF["w"], DEF["chg_lo"]):
                    pre_rows.append(row)
                    lo = lim_sim.get(k)
                    if lo is not None:
                        pre_lim[(sym, row["t_in"])] = {
                            "ret": lo[0], "reason": lo[1], "mfe": lo[3],
                            "px": float(c[k]) * (1 - LIMIT_DISCOUNT),
                            "t_in": row["t_in"],
                            "dt": datetime.fromtimestamp(int(t[k]) + HOUR + lo[2] * HOUR,
                                                         timezone.utc),
                            "t_open": int(t[k]) + HOUR,
                            "t_close": int(t[k]) + HOUR + lo[2] * HOUR}
                        # ── 升级路径：首腿 5U，chg24 摸 30% 补到 B 档 10U，
                        #    摸 50% 补到 A 档 60U（50~75% 为半仓 30U）。各腿独立出场。
                        t_exit0 = int(t[k]) + HOUR + lo[2] * HOUR
                        j_end = int(np.searchsorted(t, t_exit0, side="right"))
                        subs = [{"t_open": int(t[k]) + HOUR, "t_close": t_exit0,
                                 "notional": NOTIONAL["PRE"],
                                 "pnl": (lo[0] - COST) * NOTIONAL["PRE"]}]
                        pos_close = t_exit0
                        add = 0.0
                        seg = chg[k + 1:j_end]
                        hit = np.nonzero(seg >= 0.30)[0]
                        if len(hit):
                            jb = k + 1 + int(hit[0])
                            if chg[jb] >= 0.50:            # 一根直接跳过 B 档
                                w_a = NOTIONAL["A_HALF"] if chg[jb] < A_HALF_HI else NOTIONAL["A"]
                                add = w_a - NOTIONAL["PRE"]
                                plan = [(jb, add)]
                            else:
                                add = NOTIONAL["B"] - NOTIONAL["PRE"]
                                plan = [(jb, add)]
                                seg2 = chg[jb + 1:j_end]
                                hit2 = np.nonzero(seg2 >= 0.50)[0]
                                if len(hit2):
                                    ja = jb + 1 + int(hit2[0])
                                    w_a = (NOTIONAL["A_HALF"] if chg[ja] < A_HALF_HI
                                           else NOTIONAL["A"])
                                    plan.append((ja, w_a - NOTIONAL["B"]))
                            for jx, w in plan:
                                o2 = simulate(arr, jx, float(c[jx]), N_H, CFG, "B")
                                if o2 is None:
                                    continue
                                t2 = int(t[jx]) + HOUR
                                subs.append({"t_open": t2, "t_close": t2 + o2[2] * HOUR,
                                             "notional": w,
                                             "pnl": (o2[0] - COST) * w})
                                pos_close = max(pos_close, t2 + o2[2] * HOUR)
                        up_rows.append({"sym": sym, "t_in": row["t_in"],
                                        "t_open": int(t[k]) + HOUR, "t_close": pos_close,
                                        "pnl": sum(s["pnl"] for s in subs),
                                        "notional": max(s["notional"] for s in subs),
                                        "subs": subs})

        if nf % 50 == 0:
            print(f"  ... {nf}/{len(files)} 币，前置候选 {len(pre_rows):,}，"
                  f"A/B {len(ab_rows):,}", flush=True)

    print(f"[data] 缺文件/不可解析 {miss:,}；默认前置信号 {len(pre_rows):,}；"
          f"A/B 重算 {len(ab_rows):,}（nl {sum(r['mode'] == 'nl' for r in ab_rows):,} / "
          f"lim {sum(r['mode'] == 'lim' for r in ab_rows):,}）", flush=True)

    # ------------------------------------------------------------------ 去重（同币不叠仓）
    def no_overlap(rows: list[dict], key: str) -> list[dict]:
        out, last = [], {}
        for r in sorted(rows, key=lambda x: x["t_in"]):
            k = (r["sym"], r[key])
            if k in last and r["t_in"] < last[k]:
                continue
            out.append(r)
            last[k] = r["t_close"]
        return out

    pre_nl = pre_rows                      # 冷却 24h 已保证同币不叠仓
    ab_nl = no_overlap([r for r in ab_rows if r["mode"] == "nl"], "sig")
    ab_lim = no_overlap([r for r in ab_rows if r["mode"] == "lim"], "sig")
    pre_lim_rows = [dict(v, sym=s, sig="PRE") for (s, _), v in pre_lim.items()]

    # ------------------------------------------------------------------ Q1 独立期望
    print("\n" + "=" * 150)
    print("Q1 前置信号 vs 现 A/B 档（nl = 信号 bar 收盘立即入场；出场统一 EMA7+SL10%+24h 到期）")
    print("=" * 150)
    print(line("PRE 前置 5U", rich(pre_nl)))
    print(line("A 档 60U", rich([r for r in ab_nl if r["sig"] == "A"])))
    print(line("B 档 10U", rich([r for r in ab_nl if r["sig"] == "B"])))
    print("\n  限价口径（−0.5% / 60min，1h bar 近似）：")
    print(line("PRE 限价", rich(pre_lim_rows)))
    print(line("A 限价", rich([r for r in ab_lim if r["sig"] == "A"])))
    print(line("B 限价", rich([r for r in ab_lim if r["sig"] == "B"])))

    if CROSS_CSV.exists():
        cross = []
        with CROSS_CSV.open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["lim_filled"] != "1":
                    continue
                cross.append({"ret": float(r["lim_ret"]), "reason": r["lim_reason"],
                              "mfe": float(r["lim_mfe"]), "sig": r["sig"],
                              "dt": datetime.fromtimestamp(int(r["lim_exit"]), timezone.utc)})
        print("\n  交叉核对（已落盘 5m 限价口径 _green2_events.csv，仅验口径一致性）：")
        print(line("A 5m限价", rich([r for r in cross if r["sig"] == "A"])))
        print(line("B 5m限价", rich([r for r in cross if r["sig"] == "B"])))

    # ------------------------------------------------------------------ Q2 配对改善
    print("\n" + "=" * 150)
    print("Q2 同一波行情配对：前置腿 vs A/B 腿（前置入场须在 A/B 信号前 24h 内）")
    print("=" * 150)
    pre_by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in pre_nl:
        pre_by_sym[r["sym"]].append(r)
    for v in pre_by_sym.values():
        v.sort(key=lambda x: x["t_in"])

    pre_map = {(r["sym"], r["t_in"]): r for r in pre_nl}
    pairs = []
    for r in ab_nl:
        cands = [p for p in pre_by_sym.get(r["sym"], [])
                 if r["t_in"] - 24 * HOUR <= p["t_in"] <= r["t_in"]]
        if not cands:
            continue
        pairs.append({"sym": r["sym"], "sig": r["sig"], "pre": cands[-1], "ab": r,
                      "gap_h": (r["t_in"] - cands[-1]["t_in"]) / HOUR})

    if pairs:
        for pr in pairs:
            pr["bp"] = (pr["pre"]["px"] / pr["ab"]["px"] - 1) * 1e4
        for sig in ("A", "B"):
            sub = [p for p in pairs if p["sig"] == sig]
            if not sub:
                continue
            b = [p["bp"] for p in sub if "bp" in p]
            print(f"\n  {sig} 档：A/B 事件 {sum(1 for r in ab_nl if r['sig'] == sig):,} 条，"
                  f"有前置腿 {len(sub):,} 条（覆盖 {100*len(sub)/max(1,sum(1 for r in ab_nl if r['sig'] == sig)):.1f}%）")
            if b:
                print(f"    入场价差（前置 ÷ A/B − 1）：中位 {np.median(b):+.0f}bp，"
                      f"均值 {np.mean(b):+.0f}bp，前置更便宜占 {100*np.mean(np.array(b) < 0):.1f}%")
            print(f"    前置腿：{line('', rich([p['pre'] for p in sub]), 4).strip()}")
            print(f"    A/B 腿：{line('', rich([p['ab'] for p in sub]), 4).strip()}")
            print(f"    时间间隔中位 {np.median([p['gap_h'] for p in sub]):.1f}h；"
                  f"前置腿亏损而 A/B 腿盈利 {100*np.mean([p['pre']['ret'] <= 0.003 and p['ab']['ret'] > 0.003 for p in sub]):.1f}%，"
                  f"前置腿盈利而 A/B 腿亏损 {100*np.mean([p['pre']['ret'] > 0.003 and p['ab']['ret'] <= 0.003 for p in sub]):.1f}%")

    # Q2b：把「前置信号」当 A/B 的入场过滤器（不新增仓位，只决定 A/B 腿是否开）
    print("\n  反向用法：A/B 腿按「前 24h 内是否有前置信号」分组（不新增仓位，仅作入场过滤）")
    has_pre = {}
    for r in ab_nl:
        has_pre[(r["sym"], r["t_in"])] = any(
            r["t_in"] - 24 * HOUR <= p["t_in"] <= r["t_in"]
            for p in pre_by_sym.get(r["sym"], []))
    for sig in ("A", "B"):
        sub = [r for r in ab_nl if r["sig"] == sig]
        y = [r for r in sub if has_pre[(r["sym"], r["t_in"])]]
        n = [r for r in sub if not has_pre[(r["sym"], r["t_in"])]]
        print(f"    {sig} 有前置 {line('', rich(y), 4).strip()}")
        print(f"    {sig} 无前置 {line('', rich(n), 4).strip()}")

    # Q2c：与现行 5m 动量闸门交叉验证（外部 _green2_events.csv，5m 限价口径）
    #   现行闸门 = 信号前 30min 回落(ret30<0) 且 上根无 >2% 冲刺(last5≤2%) → 拦截
    if CROSS_CSV.exists() and pre_rows:
        print("\n  与现行 5m 动量闸门交叉（A 档，5m 限价口径）：两者是否重叠")
        pre_ts: dict[str, list[int]] = defaultdict(list)
        for r in pre_rows:
            pre_ts[r["sym"]].append(r["t_in"])
        for v in pre_ts.values():
            v.sort()
        rows5 = []
        with CROSS_CSV.open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["sig"] != "A" or r["lim_filled"] != "1":
                    continue
                try:
                    t_in = int(r["eo"]) + HOUR
                    rows5.append({
                        "has": any(t_in - 24 * HOUR <= p <= t_in
                                   for p in pre_ts.get(r["sym"], [])),
                        "gate": not (float(r["ret30"]) < 0 and float(r["last5"]) <= 0.02),
                        "ret": float(r["lim_ret"]), "reason": r["lim_reason"],
                        "mfe": float(r["lim_mfe"]), "eo": int(r["eo"]),
                        "chg24": float(r["chg24"]),
                        "dt": datetime.fromtimestamp(int(r["lim_exit"]), timezone.utc)})
                except (ValueError, TypeError):
                    continue
        if rows5:
            for has in (True, False):
                for gate in (True, False):
                    sub = [r for r in rows5 if r["has"] == has and r["gate"] == gate]
                    tag = f"{'有前置' if has else '无前置'}/{'闸门放行' if gate else '闸门拦截'}"
                    print(line(tag, rich(sub), 18))
            cur_pass = [r for r in rows5 if r["gate"]]
            both = [r for r in rows5 if r["gate"] and r["has"]]
            print(f"    现行闸门放行 {len(cur_pass):,}/{len(rows5):,}"
                  f"（{100*len(cur_pass)/len(rows5):.1f}%）；放行且叠前置特征后 "
                  f"{len(both):,}（{100*len(both)/len(rows5):.1f}%），"
                  f"再砍 {100*(1-len(both)/max(1,len(cur_pass))):.1f}%")
            print("    OOS 三段（按事件时间等分，有前置 vs 无前置，5m 限价口径）：")
            srt = sorted(rows5, key=lambda x: x["eo"])
            k3 = len(srt) // 3
            for i, seg in enumerate((srt[:k3], srt[k3:2 * k3], srt[2 * k3:]), 1):
                y = [r for r in seg if r["has"]]
                n = [r for r in seg if not r["has"]]
                sy, sn = rich(y), rich(n)
                if not (sy and sn):
                    continue
                print(f"      第{i}段 有前置 n={sy['n']:4d} 均={sy['mean']:+6.2f}% "
                      f"PF={sy['PF']:5.2f} | 无前置 n={sn['n']:4d} "
                      f"均={sn['mean']:+6.2f}% PF={sn['PF']:5.2f} | "
                      f"差={sy['mean']-sn['mean']:+5.2f}pp")
            print("    分 chg24 子档（5m 限价口径）：验证强动能档是否被误杀右尾")
            for lo_, hi_, tag in ((0.50, 0.75, "chg24 50~75%"), (0.75, 99.0, "chg24 ≥75%")):
                seg = [r for r in rows5 if lo_ <= r["chg24"] < hi_]
                y = [r for r in seg if r["has"]]
                n = [r for r in seg if not r["has"]]
                sy, sn = rich(y), rich(n)
                if not (sy and sn):
                    continue
                print(f"      {tag:<14s} 有前置 n={sy['n']:4d} 均={sy['mean']:+6.2f}% "
                      f"PF={sy['PF']:5.2f} 右尾={sy['r20']:4.1f}% | "
                      f"无前置 n={sn['n']:4d} 均={sn['mean']:+6.2f}% "
                      f"PF={sn['PF']:5.2f} 右尾={sn['r20']:4.1f}%")

    # ------------------------------------------------------------------ Q3 升级率
    print("\n" + "=" * 150)
    print("Q3 观察池转化率（前置信号后 24h 内 chg24 是否摸到下一档）")
    print("=" * 150)
    if pre_nl:
        print(f"  前置信号 {len(pre_nl):,} 条；24h 内摸到 30%(B 档) "
              f"{100*np.mean([r['up30'] for r in pre_nl]):.1f}%；"
              f"摸到 50%(A 档) {100*np.mean([r['up50'] for r in pre_nl]):.1f}%")
        print(f"  触发时 chg24 中位 {100*np.median([r['chg24'] for r in pre_nl]):.1f}%；"
              f"1h 量比中位 {np.median([r['vr'] for r in pre_nl]):.1f}")
        print("\n  期权结构拆解（前置腿 5U 单独持有，按是否升级分组）：")
        for name, sub in (("已升级 ≥30%", [r for r in pre_nl if r["up30"]]),
                          ("已升级 ≥50%", [r for r in pre_nl if r["up50"]]),
                          ("未升级 <30%", [r for r in pre_nl if not r["up30"]])):
            s = rich(sub)
            if s:
                print(line(name, s, 14) +
                      f"  单笔账户贡献={s['mean']/100*NOTIONAL['PRE']:+.3f}U")

    # ------------------------------------------------------------------ Q4 账户口径
    print("\n" + "=" * 150)
    print(f"Q4 账户口径（{ACCOUNT_U:.0f}U 实验账户，1x，限价成交口径）")
    print("=" * 150)

    def legs_ab():
        out = []
        for r in ab_lim:
            n = NOTIONAL["A"] if r["sig"] == "A" else NOTIONAL["B"]
            if r["sig"] == "A" and A_HALF_LO <= r["chg24"] < A_HALF_HI:
                n = NOTIONAL["A_HALF"]
            out.append({"sym": r["sym"], "grp": r["sig"], "t_open": r["t_open"],
                        "t_close": r["t_close"], "notional": n,
                        "pnl": (r["ret"] - COST) * n})
        return out

    def legs_pre():
        return [{"sym": r["sym"], "grp": "PRE", "t_open": r["t_open"],
                 "t_close": r["t_close"], "notional": NOTIONAL["PRE"],
                 "pnl": (r["ret"] - COST) * NOTIONAL["PRE"]}
                for r in pre_lim_rows]

    def dedupe_legs(rows: list[dict], cross: bool) -> list[dict]:
        """cross=True：同币任一腿未平仓即跳过（现行闸门）；False：仅同组压制。"""
        keep, open_ = [], {}
        for r in sorted(rows, key=lambda x: x["t_open"]):
            keys = [r["sym"]] if cross else [(r["sym"], r["grp"])]
            if any(open_.get(k, -1) > r["t_open"] for k in keys):
                continue
            keep.append(r)
            for k in keys:
                open_[k] = max(open_.get(k, -1), r["t_close"])
        return keep

    def curve(legs: list[dict], exposure: list[dict] | None = None) -> dict:
        eq = peak = mdd = 0.0
        for r in sorted(legs, key=lambda x: x["t_close"]):
            eq += r["pnl"]
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        exp = legs if exposure is None else exposure
        evs = []
        for r in exp:
            evs += [(r["t_open"], r["notional"]), (r["t_close"], -r["notional"])]
        evs.sort()
        cur = area = mx = 0.0
        prev = evs[0][0] if evs else 0
        for ts, d in evs:
            area += cur * (ts - prev)
            cur += d
            prev = ts
            mx = max(mx, cur)
        span = (evs[-1][0] - evs[0][0]) if len(evs) > 1 else 1
        return {"n": len(legs), "pnl": eq, "mdd": mdd,
                "avg_notional": area / span if span else 0.0, "max_notional": mx,
                "win": 100 * np.mean([r["pnl"] > 0 for r in legs]) if legs else 0.0}

    ab_l, pre_l = legs_ab(), legs_pre()
    base_legs = dedupe_legs(ab_l, True)
    strict_legs = dedupe_legs(ab_l + pre_l, True)
    add_legs = dedupe_legs(ab_l, True) + dedupe_legs(pre_l, False)

    # 升级路径：前置腿开仓后，A/B 信号不再独立开仓（被补仓替代），只保留未被覆盖的 A/B 腿
    up_pos = dedupe_legs([dict(r, grp="UP") for r in up_rows], False)
    up_by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in up_pos:
        up_by_sym[r["sym"]].append(r)
    kept_ab, replaced = [], 0
    for r in base_legs:
        hit = [p for p in up_by_sym.get(r["sym"], [])
               if p["t_open"] <= r["t_open"] < p["t_close"]]
        if hit:
            replaced += 1
        else:
            kept_ab.append(r)
    up_legs = kept_ab + up_pos
    up_exposure = [s for p in up_pos for s in p["subs"]] + kept_ab

    base = curve(base_legs)
    strict = curve(strict_legs)
    additive = curve(add_legs)
    upgrade = curve(up_legs, up_exposure)
    pre_only = curve(dedupe_legs(pre_l, False))
    for name, s in (("A+B 现状", base),
                    ("+PRE 5U（现行闸门：前置腿占位，A/B 被挡）", strict),
                    ("+PRE 5U（允许同币叠仓，A/B 照常开）", additive),
                    ("升级路径（5U→B 10U→A 60U，替代 A/B 腿）", upgrade),
                    ("PRE 单独（5U）", pre_only)):
        print(f"  {name:<40s} n={s['n']:5d} 胜={s['win']:5.1f}% "
              f"总盈亏={s['pnl']:+7.1f}U（{100*s['pnl']/ACCOUNT_U:+6.1f}%账户） "
              f"最大回撤={s['mdd']:+7.1f}U 平均并发名义={s['avg_notional']:5.1f}U "
              f"峰值并发={s['max_notional']:5.1f}U")
    print(f"  升级路径替代 A/B 腿 {replaced:,} 条；前置腿时间加权仓位 / A+B = "
          f"{100*(additive['avg_notional']-base['avg_notional'])/max(base['avg_notional'],1e-9):.1f}%"
          f"（用户约束 ≤30%）")

    # ------------------------------------------------------------------ 敏感性
    if not args.no_grid:
        print("\n" + "=" * 150)
        print("Q5 参数敏感性（nl 口径，均含 24h 冷却；chg_hi 固定 30%）")
        print("=" * 150)
        print(f"  {'vr':>4} {'窗口':>4} {'chg门槛':>7} {'n':>6} {'胜%':>6} {'均%':>7} "
              f"{'PF':>6} {'右尾%':>6} {'止损%':>6} {'升级30%':>7} {'升级50%':>7} "
              f"{'5U累计U':>8}")
        for v, w, g in combos:
            rows = grid_rows[(v, w, g)]
            s = rich(rows)
            if not s:
                continue
            pnl = sum((r["ret"] - 0.003) * NOTIONAL["PRE"] for r in rows)
            print(f"  {v:>4.0f} {w:>4d} {100*g:>6.0f}% {s['n']:>6d} {s['win']:>6.1f} "
                  f"{s['mean']:>+7.2f} {s['PF']:>6.2f} {s['r20']:>6.1f} {s['sl']:>6.1f} "
                  f"{100*np.mean([r['up30'] for r in rows]):>7.1f} "
                  f"{100*np.mean([r['up50'] for r in rows]):>7.1f} {pnl:>+8.1f}")

    # ------------------------------------------------------------------ 落盘
    if pre_rows:
        cols = ["sym", "t_in", "chg24", "vr", "ret", "reason", "mfe", "up30", "up50"]
        with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
            w_ = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w_.writeheader()
            w_.writerows(pre_rows)
        print(f"\n[out] {OUT_CSV}（默认参数 {len(pre_rows):,} 条，nl 口径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
