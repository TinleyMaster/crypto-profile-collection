#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""深度缩量 × OI 方向 × 价格方向 组合回测（2026-10-07）

背景：§3.10 发现深度缩量（VOL_ratio<0.30）是迄今最强单因子（12h 超额 +1.18%）。
本脚本检验深度缩量叠加杠杆状态（OI 方向）与价格方向（跌后/涨后）后是否增强：

假设：
  H1 深缩 + OI 降（平仓出清） > 深缩 + OI 增（无成交杠杆堆积）
  H2 深缩 + 跌后（抛压耗尽）  > 深缩 + 涨后（上涨缩量/假突破）
  H3 深缩 + OI 降 + 跌后       是全局最强反弹组合

输出（超额=减 BTC，均含 0.3% 成本线对照）：
  1) 深度缩量 × OI 方向      （4 组：深缩/非深缩 × OI增/OI降）
  2) 深度缩量 × 价格方向     （4 组：深缩/非深缩 × 跌后/涨后）
  3) 三维 8 组               （深缩/非深缩 × OI × P）

用法：
  python research_cg_vol_deep_combo.py --interval 12h
  python research_cg_vol_deep_combo.py --interval 4h
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
MIN_BUCKETS = {"4h": 60, "12h": 30}
MIN_N = 30
MIN_MED_ABS_RET = 0.003
DEEP_VOL = 0.30          # 深度缩量阈值（§3.10 有效阈值）
COST = 0.003             # 往返成本


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


def build_panel(oi_rows: list, cvd_rows: list, klines: dict[str, dict], interval: str) -> list[dict]:
    iv_h = int(interval[:-1]) if interval.endswith("h") else 4

    oi_ratio: dict[tuple, float] = {}
    for sym, ts, o, c in oi_rows:
        o, c = float(o), float(c)
        if o > 0:
            oi_ratio[(sym, ts)] = c / o - 1.0
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

    keys: dict[str, set] = defaultdict(set)
    for (sym, ts) in oi_ratio:
        keys[sym].add(ts)
    for (sym, ts) in cvd_net:
        keys[sym].add(ts)

    panel: list[dict] = []
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
            if ts not in idx or (sym, ts) not in oi_ratio or (sym, ts) not in cvd_net:
                continue
            if idx[ts] + iv_h + max(HORIZONS[interval]) >= n:
                continue
            i_entry = idx[ts] + iv_h
            entry_px = px[hours[i_entry]]
            ts_px = px[hours[idx[ts]]]
            if entry_px <= 0 or ts_px <= 0:
                continue
            p_ret = entry_px / ts_px - 1.0        # 桶内价格方向（相对桶起始）
            fwd: dict = {}
            for h in HORIZONS[interval]:
                fwd[h] = px[hours[i_entry + h]] / entry_px - 1.0
            panel.append({
                "symbol": sym, "bucket": ts,
                "p_dir": 1 if p_ret > 0 else 0,
                "oi_chg": oi_ratio[(sym, ts)],    # 桶内 OI 变化率
                "vr": cvd_vol[(sym, ts)] / vm,    # 量能比
                "fwd": fwd, "btc_fwd": btc_fwd.get(hours[i_entry], {}),
            })
    return panel


def _ex(r: dict, h: int) -> float | None:
    b = r["fwd"].get(h)
    btc = r["btc_fwd"].get(h)
    if b is None or btc is None:
        return None
    return b - btc


def report_group(rows: list[dict], label: str, hlast: int) -> None:
    if len(rows) < MIN_N:
        print(f"  {label:<28} n={len(rows):>7}  样本不足")
        return
    ex = [_ex(r, hlast) for r in rows if _ex(r, hlast) is not None]
    if len(ex) < MIN_N:
        print(f"  {label:<28} n={len(rows):>7}  超额样本不足")
        return
    m = statistics.mean(ex)
    win = sum(1 for x in ex if x > 0) / len(ex)
    net = m - COST
    flag = "✅" if net > 0 else "❌"
    print(f"  {label:<28} n={len(rows):>7}  超额 {m*100:+6.2f}%  胜率 {win*100:3.0f}%  "
          f"净 {net*100:+6.2f}%  {flag}")


def main() -> None:
    ap = argparse.ArgumentParser(description="深度缩量 × OI × 价格方向 组合回测")
    ap.add_argument("--interval", choices=("4h", "12h"), default="12h")
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

    panel = build_panel(oi_rows, cvd_rows, klines, args.interval)
    hlast = HORIZONS[args.interval][-1]
    print(f"粒度={args.interval} | 面板 {len(panel)} 行 / {len({r['symbol'] for r in panel})} 币")
    print(f"深度缩量阈值 VOL_ratio<{DEEP_VOL}，成本 {COST*100:.1f}%，最远窗 H{hlast}h\n")

    deep = [r for r in panel if r["vr"] < DEEP_VOL]
    notdeep = [r for r in panel if r["vr"] >= DEEP_VOL]
    oi_up = [r for r in panel if r["oi_chg"] > 0]
    oi_dn = [r for r in panel if r["oi_chg"] <= 0]
    p_up = [r for r in panel if r["p_dir"] == 1]
    p_dn = [r for r in panel if r["p_dir"] == 0]
    print(f"  基线 n={len(panel)}  深缩 {len(deep)}  OI增 {len(oi_up)}  OI降 {len(oi_dn)}  "
          f"涨后 {len(p_up)}  跌后 {len(p_dn)}\n")

    # ── 1) 深度缩量 × OI 方向 ──
    print("=== 1) 深度缩量 × OI 方向 ===")
    print("  深缩+OI增 = 无成交杠杆堆积 | 深缩+OI降 = 平仓出清")
    report_group([r for r in deep if r["oi_chg"] > 0], "深缩 + OI 增", hlast)
    report_group([r for r in deep if r["oi_chg"] <= 0], "深缩 + OI 降", hlast)
    report_group([r for r in notdeep if r["oi_chg"] > 0], "非深缩 + OI 增", hlast)
    report_group([r for r in notdeep if r["oi_chg"] <= 0], "非深缩 + OI 降", hlast)

    # ── 2) 深度缩量 × 价格方向 ──
    print("\n=== 2) 深度缩量 × 价格方向 ===")
    print("  跌后缩量 = 抛压耗尽 | 涨后缩量 = 上涨缩量/假突破")
    report_group([r for r in deep if r["p_dir"] == 1], "深缩 + 涨后", hlast)
    report_group([r for r in deep if r["p_dir"] == 0], "深缩 + 跌后", hlast)
    report_group([r for r in notdeep if r["p_dir"] == 1], "非深缩 + 涨后", hlast)
    report_group([r for r in notdeep if r["p_dir"] == 0], "非深缩 + 跌后", hlast)

    # ── 3) 三维 8 组 ──
    print("\n=== 3) 三维 8 组（深缩/非深缩 × OI × P） ===")
    combos = [
        ("深缩+OI增+涨后", lambda r: r["vr"] < DEEP_VOL and r["oi_chg"] > 0 and r["p_dir"] == 1),
        ("深缩+OI增+跌后", lambda r: r["vr"] < DEEP_VOL and r["oi_chg"] > 0 and r["p_dir"] == 0),
        ("深缩+OI降+涨后", lambda r: r["vr"] < DEEP_VOL and r["oi_chg"] <= 0 and r["p_dir"] == 1),
        ("深缩+OI降+跌后", lambda r: r["vr"] < DEEP_VOL and r["oi_chg"] <= 0 and r["p_dir"] == 0),
        ("非深缩+OI增+涨后", lambda r: r["vr"] >= DEEP_VOL and r["oi_chg"] > 0 and r["p_dir"] == 1),
        ("非深缩+OI增+跌后", lambda r: r["vr"] >= DEEP_VOL and r["oi_chg"] > 0 and r["p_dir"] == 0),
        ("非深缩+OI降+涨后", lambda r: r["vr"] >= DEEP_VOL and r["oi_chg"] <= 0 and r["p_dir"] == 1),
        ("非深缩+OI降+跌后", lambda r: r["vr"] >= DEEP_VOL and r["oi_chg"] <= 0 and r["p_dir"] == 0),
    ]
    for label, pred in combos:
        report_group([r for r in panel if pred(r)], label, hlast)

    print("\n完成。")


if __name__ == "__main__":
    main()
