#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盘面因子组合净值回测（2026-10-07）

把已确认的弱信号合成真实策略，跑 360 天净值曲线——最终回答「能不能用」。

策略规则（全部来自 §3.6~§3.11 已验证结论，无前视）：
  信号（每 12h 桶末扫描全市场）：
    LONG  = 深缩：VOL_ratio < 0.30                      （§3.10，净 +0.88%/4 天）
    SHORT = 放量 S3：VOL_ratio ≥ 1.0 且 P↓ OI↑ CVD↓     （§3.7/3.8，净 +0.69%/4 天）
  组合规则：
    - 每 12h 桶末开仓，持仓固定 H=96h（4 天）
    - 多币等权：每仓权重 = 总权益 / max_positions（默认 8）
    - 同一币不可重复持仓（未平仓前忽略新信号）
    - 成本 0.3% 往返（平仓时一次性扣除）
    - 现金不足跳过开仓（记录资金受限次数）
  输出：净值曲线、年化、最大回撤、夏普（12h 收益序列×√730）、
        平仓笔数、胜率、多空分列、月度收益、对比 BTC 买入持有。

用法：
  python research_cg_combo_equity.py --interval 12h --max-pos 8 --cost 0.003
  python research_cg_combo_equity.py --interval 4h  --max-pos 12
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = {"4h": (4, 8, 12, 24, 48), "12h": (12, 24, 48, 72, 96)}
MIN_BUCKETS = {"4h": 60, "12h": 30}
MIN_MED_ABS_RET = 0.003
DEEP_VOL = 0.30          # 深缩阈值
S3_VOL = 1.0             # S3 放量阈值


def load_oi(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ts, oi_open, oi_close FROM biz.cg_oi_hist "
            "WHERE interval=%s AND oi_open IS NOT NULL AND oi_close IS NOT NULL AND oi_open>0 "
            "ORDER BY symbol, ts", (interval,))
        return cur.fetchall()


def load_cvd(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ts, taker_buy_usd, taker_sell_usd FROM biz.cg_taker_volume_hist "
            "WHERE interval=%s AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL "
            "ORDER BY symbol, ts", (interval,))
        return cur.fetchall()


def load_klines_1h(conn, symbols: set[str], lo_ts) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, open_time, close_px FROM biz.asset_klines "
            "WHERE interval='1h' AND open_time >= %s::timestamp AND symbol = ANY(%s::text[]) "
            "ORDER BY symbol, open_time", (lo_ts, list(symbols)))
        for sym, ot, close in cur.fetchall():
            if close is None or float(close) <= 0:
                continue
            out.setdefault(sym, {})[ot] = float(close)
    return out


def build_signals(oi_rows: list, cvd_rows: list, klines: dict[str, dict],
                  interval: str) -> tuple[dict, list]:
    """生成 {entry_hour: [(symbol, dir, entry_px)]} 信号表 + 时间轴。"""
    iv_h = int(interval[:-1]) if interval.endswith("h") else 4

    oi_chg: dict[tuple, float] = {}
    for sym, ts, o, c in oi_rows:
        o, c = float(o), float(c)
        if o > 0:
            oi_chg[(sym, ts)] = c / o - 1.0
    cvd_net: dict[tuple, float] = {}
    cvd_vol: dict[tuple, float] = {}
    for sym, ts, b, sl in cvd_rows:
        b, sl = float(b), float(sl)
        cvd_net[(sym, ts)] = b - sl
        cvd_vol[(sym, ts)] = b + sl

    vol_by_sym: dict[str, list[float]] = defaultdict(list)
    for (sym, ts), v in cvd_vol.items():
        vol_by_sym[sym].append(v)
    vol_med: dict[str, float] = {sym: statistics.median(vs) for sym, vs in vol_by_sym.items() if vs}

    keys: dict[str, set] = defaultdict(set)
    for (sym, ts) in oi_chg:
        keys[sym].add(ts)
    for (sym, ts) in cvd_net:
        keys[sym].add(ts)

    signals: dict = defaultdict(list)
    timeline: set = set()
    for sym, tss in keys.items():
        px = klines.get(sym)
        if not px:
            continue
        hours = sorted(px)
        idx = {d: i for i, d in enumerate(hours)}
        n = len(hours)
        rets = []
        for i in range(1, n):
            if px[hours[i - 1]] > 0:
                rets.append(abs(px[hours[i]] / px[hours[i - 1]] - 1.0))
        if rets and statistics.median(rets) < MIN_MED_ABS_RET:
            continue
        vm = vol_med.get(sym)
        if vm is None or vm <= 0:
            continue
        for ts in sorted(tss):
            if ts not in idx or (sym, ts) not in oi_chg or (sym, ts) not in cvd_net:
                continue
            if idx[ts] + iv_h + HORIZONS[interval][-1] >= n:
                continue
            i_entry = idx[ts] + iv_h
            entry_px = px[hours[i_entry]]
            ts_px = px[hours[idx[ts]]]
            if entry_px <= 0 or ts_px <= 0:
                continue
            p_ret = entry_px / ts_px - 1.0
            vr = cvd_vol[(sym, ts)] / vm
            entry_hour = hours[i_entry]
            # LONG：深缩
            if vr < DEEP_VOL:
                signals[entry_hour].append((sym, 1, entry_px))
                timeline.add(entry_hour)
            # SHORT：放量 S3（P↓ OI↑ CVD↓）
            elif vr >= S3_VOL and p_ret <= 0 and oi_chg[(sym, ts)] > 0 and cvd_net[(sym, ts)] <= 0:
                signals[entry_hour].append((sym, -1, entry_px))
                timeline.add(entry_hour)
    return signals, sorted(timeline)


def max_drawdown(equities: list[float]) -> float:
    peak = -1e18
    mdd = 0.0
    for e in equities:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak)
    return mdd


def run_backtest(signals: dict, timeline: list, klines: dict[str, dict],
                 interval: str, max_pos: int, cost: float) -> dict:
    hold_h = HORIZONS[interval][-1]
    hold_delta = timedelta(hours=hold_h)
    cash = 1.0
    positions: list[dict] = []          # {sym, entry_t, dir, weight, entry_px}
    pos_syms: set = set()
    closed: list[float] = []
    closed_by_dir: dict[int, list] = {1: [], -1: []}
    equity_curve: list[tuple] = []
    skipped = 0
    marks: dict[str, dict] = klines

    def price_now(sym: str, t) -> float:
        px = marks.get(sym, {})
        if t in px:
            return px[t]
        hs = sorted(px)
        if not hs:
            return float("nan")
        if t < hs[0]:
            return px[hs[0]]
        lo_i = 0
        for i, h in enumerate(hs):
            if h <= t:
                lo_i = i
        return px[hs[lo_i]]

    for t in timeline:
        # 1) 平仓到期
        due = [p for p in positions if p["entry_t"] + hold_delta <= t]
        for p in due:
            now = price_now(p["sym"], t)
            ret = (now / p["entry_px"] - 1.0) * p["dir"]
            pnl = p["weight"] * ret
            cash += p["weight"] * (1 + ret - cost)     # 扣往返成本
            closed.append(pnl)
            closed_by_dir[p["dir"]].append(pnl)
            pos_syms.discard(p["sym"])
            positions.remove(p)
        # 2) 记录净值（含持仓市值）
        mkt = 0.0
        for p in positions:
            now = price_now(p["sym"], t)
            ret = (now / p["entry_px"] - 1.0) * p["dir"]
            mkt += p["weight"] * (1 + ret)
        equity_curve.append((t, cash + mkt))
        # 3) 开新仓（等权，最多 max_pos）
        total_eq = cash + mkt
        for sym, dir_, entry_px in signals.get(t, []):
            if sym in pos_syms:
                continue
            w = total_eq / max_pos
            if cash < w:
                skipped += 1
                continue
            positions.append({"sym": sym, "entry_t": t, "dir": dir_,
                              "weight": w, "entry_px": entry_px})
            pos_syms.add(sym)
            cash -= w
            if len(positions) >= max_pos:
                break

    # 收尾：按最后已知价强制平仓
    t_end = timeline[-1]
    for p in positions:
        now = price_now(p["sym"], t_end)
        ret = (now / p["entry_px"] - 1.0) * p["dir"]
        cash += p["weight"] * (1 + ret - cost)
        closed.append(p["weight"] * ret)
        closed_by_dir[p["dir"]].append(p["weight"] * ret)
    positions.clear()
    equity_curve.append((t_end, cash))

    eq = [e for _, e in equity_curve]
    n_pts = len(eq)
    total = eq[-1] / eq[0] - 1.0
    # 年化：桶数→年（12h=730桶/年，4h=2190桶/年）
    buckets_per_year = 365 * 24 // HORIZONS[interval][0]
    years = n_pts / buckets_per_year
    annual = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[0] > 0 else float("nan")
    # 桶收益序列 → 夏普
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq)) if eq[i - 1] > 0]
    if len(rets) > 2 and statistics.stdev(rets) > 0:
        sharpe = statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(buckets_per_year)
    else:
        sharpe = float("nan")
    win = sum(1 for x in closed if x > 0) / len(closed) if closed else float("nan")
    return {
        "total": total, "annual": annual, "mdd": max_drawdown(eq), "sharpe": sharpe,
        "n_closed": len(closed), "win": win,
        "n_long": len(closed_by_dir[1]), "n_short": len(closed_by_dir[-1]),
        "win_long": (sum(1 for x in closed_by_dir[1] if x > 0) / len(closed_by_dir[1])
                     if closed_by_dir[1] else float("nan")),
        "win_short": (sum(1 for x in closed_by_dir[-1] if x > 0) / len(closed_by_dir[-1])
                      if closed_by_dir[-1] else float("nan")),
        "skipped": skipped, "n_pts": n_pts, "years": years,
        "end_equity": eq[-1], "btc_ret": None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="盘面因子组合净值回测")
    ap.add_argument("--interval", choices=("4h", "12h"), default="12h")
    ap.add_argument("--max-pos", type=int, default=8, help="最大并行仓位数")
    ap.add_argument("--cost", type=float, default=0.003, help="往返成本")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        oi_rows = load_oi(conn, args.interval)
        cvd_rows = load_cvd(conn, args.interval)
        syms = {r[0] for r in oi_rows} | {r[0] for r in cvd_rows} | {"BTCUSDT"}
        lo = min([r[1] for r in oi_rows + cvd_rows], default=None)
        if lo is None:
            print("无数据")
            return
        klines = load_klines_1h(conn, syms, lo)

    signals, timeline = build_signals(oi_rows, cvd_rows, klines, args.interval)
    n_long = sum(1 for t in timeline for sym, d, _ in signals.get(t, []) if d == 1)
    n_short = sum(1 for t in timeline for sym, d, _ in signals.get(t, []) if d == -1)
    print(f"粒度={args.interval} | 最大并行={args.max_pos} | 成本={args.cost*100:.1f}% | "
          f"持仓=H{HORIZONS[args.interval][-1]}h")
    print(f"信号量：LONG(深缩) {n_long} 条 / SHORT(S3放量) {n_short} 条 / 时间轴 {len(timeline)} 点\n")

    r = run_backtest(signals, timeline, klines, args.interval, args.max_pos, args.cost)
    print("=== 组合净值结果 ===")
    print(f"  区间长度         {r['years']:.2f} 年（{r['n_pts']} 个桶末点）")
    print(f"  期末净值         {r['end_equity']:.3f}")
    print(f"  总收益           {r['total']*100:+.1f}%")
    print(f"  年化收益         {r['annual']*100:+.1f}%")
    print(f"  最大回撤         {r['mdd']*100:.1f}%")
    print(f"  夏普（桶×√年桶数） {r['sharpe']:.2f}")
    print(f"  平仓笔数         {r['n_closed']}（多 {r['n_long']} / 空 {r['n_short']}）")
    print(f"  总体胜率         {r['win']*100:.1f}%（多 {r['win_long']*100:.1f}% / 空 {r['win_short']*100:.1f}%）")
    print(f"  资金受限跳过     {r['skipped']} 次")

    # BTC 基准
    btc = klines.get("BTCUSDT", {})
    btc_h = sorted(btc)
    if btc_h and timeline:
        t0 = timeline[0]
        t1 = timeline[-1]
        p0 = btc.get(t0) or btc[btc_h[0]]
        p1 = btc.get(t1) or btc[btc_h[-1]]
        print(f"  BTC 同期买入持有   {(p1/p0-1)*100:+.1f}%")

    # 月度收益
    print("\n=== 月度净值 ===")
    month_key: dict = {}
    for t, e in equity_curve_for_month(signals, timeline, klines, args.interval, args.max_pos, args.cost):
        month_key.setdefault(t.strftime("%Y-%m"), []).append(e)
    prev = None
    for mk in sorted(month_key):
        last = month_key[mk][-1]
        if prev:
            print(f"  {mk}  {last/prev-1:+.2%}")
        prev = last
    print("\n完成。")


def equity_curve_for_month(signals, timeline, klines, interval, max_pos, cost):
    """轻量重算净值曲线（返回 (t, equity) 列表，供月度聚合）。"""
    hold_h = HORIZONS[interval][-1]
    hold_delta = timedelta(hours=hold_h)
    cash = 1.0
    positions: list[dict] = []
    pos_syms: set = set()
    marks = klines

    def price_now(sym: str, t) -> float:
        px = marks.get(sym, {})
        if t in px:
            return px[t]
        hs = sorted(px)
        if not hs:
            return float("nan")
        lo_i = 0
        for i, h in enumerate(hs):
            if h <= t:
                lo_i = i
        return px[hs[lo_i]]

    out = []
    for t in timeline:
        due = [p for p in positions if p["entry_t"] + hold_delta <= t]
        for p in due:
            now = price_now(p["sym"], t)
            ret = (now / p["entry_px"] - 1.0) * p["dir"]
            cash += p["weight"] * (1 + ret - cost)
            pos_syms.discard(p["sym"])
            positions.remove(p)
        mkt = 0.0
        for p in positions:
            now = price_now(p["sym"], t)
            ret = (now / p["entry_px"] - 1.0) * p["dir"]
            mkt += p["weight"] * (1 + ret)
        total_eq = cash + mkt
        out.append((t, total_eq))
        for sym, dir_, entry_px in signals.get(t, []):
            if sym in pos_syms:
                continue
            w = total_eq / max_pos
            if cash < w:
                continue
            positions.append({"sym": sym, "entry_t": t, "dir": dir_,
                              "weight": w, "entry_px": entry_px})
            pos_syms.add(sym)
            cash -= w
    return out


if __name__ == "__main__":
    main()
