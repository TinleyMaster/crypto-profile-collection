#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S3/S4 场景 × OI 增幅强度分档回测（2026-10-07）。

背景：S1-S8 全样本八象限回测显示 S3（真实空头）12h 超额全负、S4（诱空反弹）
12h 超额强正，但均值收益仅 ±0.2~0.5%，扣 0.3% 往返成本后无利可图。
本脚本验证核心假设：「拥挤回落」效应是否集中在 OI 增幅的**极端桶**——即
S3 的 OI 增幅越强 → 后续越跌；S4 的 OI 增幅越强 → 反弹越强。若右尾桶净收益
能覆盖成本，则框架可作为信号；否则只配作状态标签。

口径：
  - 因子桶 [T, T+iv) → 入场 = 桶末价（无前视）→ fwd_h = close(T+iv+h)/close(T+iv)-1
  - 场景（用户口径）：S3 = P↓ OI↑ CVD↓（真实空头）；S4 = P↓ OI↑ CVD↑（诱空/现货承接）
  - OI 增幅 = oi_close/oi_open - 1，按全面板 OI↑ 桶的增幅分 5 档（Q1 最弱 … Q5 最强）
  - VOL↑ = 桶 taker 总额 ≥ 该币中位数（相对放量，场景前提）
  - 交易化净收益：S3 做空 = -fwd_h - cost；S4 做多 = fwd_h - cost；cost = 0.15%/单边 × 2 = 0.30% 往返
  - 稳健性：超额（减 BTC 同期）重复

用法：
  python research_cg_s1s8_strength.py --interval 12h
  python research_cg_s1s8_strength.py --interval 4h --cost 0.003
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

HORIZONS = {"4h": (4, 8, 12, 24, 48), "12h": (12, 24, 48, 72, 96)}
MIN_N = 30
MIN_MED_ABS_RET = 0.003
DEFAULT_COST = 0.003       # 往返成本（默认 0.3%）
NB = 5                     # 强度分档数


# ── 数据加载（与 research_cg_s1s8_backtest.py 同源）──────────

def load_oi(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, oi_open, oi_close
            FROM biz.cg_oi_hist
            WHERE interval = %s AND oi_open IS NOT NULL AND oi_close IS NOT NULL AND oi_open > 0
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_cvd(conn, interval: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, ts, taker_buy_usd, taker_sell_usd
            FROM biz.cg_taker_volume_hist
            WHERE interval = %s AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL
            ORDER BY symbol, ts
            """, (interval,))
        return cur.fetchall()


def load_klines_1h(conn, symbols: set[str], lo_ts) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, open_time, close_px
            FROM biz.asset_klines
            WHERE interval = '1h' AND open_time >= %s::timestamp
              AND symbol = ANY(%s::text[])
            ORDER BY symbol, open_time
            """, (lo_ts, list(symbols)))
        for sym, ot, close in cur.fetchall():
            if close is None or float(close) <= 0:
                continue
            out.setdefault(sym, {})[ot] = float(close)
    return out


# ── 面板构建 ──────────────────────────────────────────────────

def build_panel(oi_rows: list, cvd_rows: list, klines: dict[str, dict],
                interval: str) -> list[dict]:
    """构建 S3/S4 场景面板（含 OI 增幅数值，供强度分档）。"""
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

    # BTC 基准
    btc_id = "BTCUSDT"
    btc_px = klines.get(btc_id, {})
    btc_hours = sorted(btc_px)
    bn = len(btc_hours)
    btc_fwd: dict = {}
    for i, d in enumerate(btc_hours):
        btc_fwd[d] = {}
        for h in HORIZONS[interval]:
            if i + h < bn and btc_px[btc_hours[i]] > 0:
                btc_fwd[d][h] = btc_px[btc_hours[i + h]] / btc_px[btc_hours[i]] - 1.0

    keys_by_sym: dict[str, set] = defaultdict(set)
    for (sym, ts) in oi_chg:
        keys_by_sym[sym].add(ts)
    for (sym, ts) in cvd_net:
        keys_by_sym[sym].add(ts)

    panel: list[dict] = []
    for sym, tss in keys_by_sym.items():
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
            if idx[ts] + iv_h + max(HORIZONS[interval]) >= n:
                continue
            i_entry = idx[ts] + iv_h
            entry_px = px[hours[i_entry]]
            if entry_px <= 0:
                continue
            # 仅保留 S3（P↓OI↑CVD↓）/ S4（P↓OI↑CVD↑）且 VOL↑
            p_dir = 1 if entry_px / px[hours[idx[ts]]] - 1.0 > 0 else 0
            o = oi_chg[(sym, ts)]
            o_dir = 1 if o > 0 else 0
            c_dir = 1 if cvd_net[(sym, ts)] > 0 else 0
            if p_dir != 0 or o_dir != 1:      # 必须 P↓ 且 OI↑
                continue
            if cvd_vol[(sym, ts)] < vm:        # VOL↑ 前提
                continue
            fwd: dict = {}
            for h in HORIZONS[interval]:
                fwd[h] = px[hours[i_entry + h]] / entry_px - 1.0
            panel.append({
                "symbol": sym, "bucket": ts,
                "scene": "S3" if c_dir == 0 else "S4",
                "oi_chg": o, "fwd": fwd,
                "btc_fwd": btc_fwd.get(hours[i_entry], {}),
            })
    return panel


# ── 输出 ──────────────────────────────────────────────────────

def stats_of(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    mean = sum(rets) / len(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "win": win}


def _y(r: dict, h: int, use_excess: bool, side: str) -> float | None:
    """side='short'（S3 做空）取 -fwd；side='long'（S4 做多）取 +fwd。"""
    b = r["fwd"].get(h)
    if b is None:
        return None
    if use_excess:
        btc = r["btc_fwd"].get(h)
        if btc is None:
            return None
        v = b - btc
    else:
        v = b
    return -v if side == "short" else v


def print_strength(panel: list[dict], title: str, use_excess: bool, cost: float) -> None:
    hs = HORIZONS["4h" if "4h" in title else "12h"]
    print(f"\n=== {title} ===")
    for scene, side, label in (
            ("S3", "short", "S3 真实空头 · 做空（-fwd）"),
            ("S4", "long", "S4 诱空反弹 · 做多（+fwd）")):
        rows = [r for r in panel if r["scene"] == scene]
        if not rows:
            print(f"  {label}: 无样本")
            continue
        chgs = sorted(r["oi_chg"] for r in rows)
        if len(chgs) < NB:
            print(f"  {label}: 样本不足（n={len(chgs)} < {NB}）无法分档")
            continue
        qs = [chgs[min(int(len(chgs) * i / NB), len(chgs) - 1)] for i in range(NB + 1)]
        print(f"\n  —— {label}（n={len(rows)}）按 OI 增幅分 {NB} 档 ——")
        print(f"    {'档':<4} {'OI增幅区间':<16} {'n':>6} | "
              + " | ".join(f"H{h}h" for h in hs))
        for i in range(NB):
            lo, hi = qs[i], qs[i + 1]
            sub = [r for r in rows if lo <= r["oi_chg"] < hi] if i < NB - 1 else \
                  [r for r in rows if r["oi_chg"] >= lo]
            cells = []
            for h in hs:
                st = stats_of([_y(r, h, use_excess, side) for r in sub if _y(r, h, use_excess, side) is not None])
                if st["n"] >= MIN_N:
                    cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%")
                else:
                    cells.append("  --/--")
            print(f"    Q{i+1}   [{lo*100:+5.2f},{hi*100:+5.2f})  {len(sub):>6} | " + " | ".join(cells))
        # 净收益（扣成本）：用最远档 h 计算各档 net
        hlast = hs[-1]
        print(f"\n    净收益（往返成本 {cost*100:.2f}%，H{hlast}h）：")
        print(f"    {'档':<4} {'毛收益':>8} {'-成本':>8} {'超额净':>8}  说明")
        for i in range(NB):
            lo, hi = qs[i], qs[i + 1]
            sub = [r for r in rows if lo <= r["oi_chg"] < hi] if i < NB - 1 else \
                  [r for r in rows if r["oi_chg"] >= lo]
            gross = statistics.mean([_y(r, hlast, False, side) for r in sub
                                     if _y(r, hlast, False, side) is not None])
            net = gross - cost
            ex = statistics.mean([_y(r, hlast, True, side) for r in sub
                                  if _y(r, hlast, True, side) is not None])
            flag = ""
            if net > 0 and ex > 0:
                flag = "✅ 覆盖成本"
            elif net > 0:
                flag = "▲ 毛正"
            print(f"    Q{i+1}   {gross*100:+7.2f}% {net*100:+7.2f}% {ex*100:+7.2f}%  {flag}")


def main() -> None:
    ap = argparse.ArgumentParser(description="S3/S4 × OI 增幅强度分档回测")
    ap.add_argument("--interval", choices=("4h", "12h"), default="12h")
    ap.add_argument("--cost", type=float, default=DEFAULT_COST, help="往返成本（默认 0.003）")
    args = ap.parse_args()

    s = get_settings()
    with get_connection(s.database_url) as conn:
        oi_rows = load_oi(conn, args.interval)
        cvd_rows = load_cvd(conn, args.interval)
        syms = {r[0] for r in oi_rows} | {r[0] for r in cvd_rows} | {"BTCUSDT"}
        lo = min([r[1] for r in oi_rows + cvd_rows], default=None)
        if lo is None:
            print("无 Coinglass 历史数据")
            return
        klines = load_klines_1h(conn, syms, lo)

    print(f"粒度={args.interval} | oi={len(oi_rows)} cvd={len(cvd_rows)} | 成本={args.cost*100:.2f}% 往返")
    panel = build_panel(oi_rows, cvd_rows, klines, args.interval)
    n_s3 = sum(1 for r in panel if r["scene"] == "S3")
    n_s4 = sum(1 for r in panel if r["scene"] == "S4")
    print(f"面板：S3={n_s3} 桶，S4={n_s4} 桶（VOL↑ 且 P↓OI↑）")

    print_strength(panel, f"S3/S4 × OI 强度分档（{args.interval}）", use_excess=False, cost=args.cost)
    print_strength(panel, f"稳健性：超额(减BTC)（{args.interval}）", use_excess=True, cost=args.cost)

    print("\n完成。")


if __name__ == "__main__":
    main()
