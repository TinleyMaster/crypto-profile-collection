#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VOL 量能强度分档回测：缩量要多缩才有效？（2026-10-07）

背景：§3.9 发现缩量桶后续收益显著高于放量桶（12h 基线超额 +0.39% vs +0.01%）。
本脚本量化「缩量深度 → 收益」的关系，回答两个问题：
  1) 缩量是否单调增强（越缩越有效）？
  2) 从哪个量能比阈值起「缩量效应」才显著（>成本线 0.3%）？

口径：
  - VOL_ratio = 桶 taker 总额（buy+sell）/ 该币自身中位数（相对量能比）
    · <1 = 缩量；>1 = 放量；≈0.5 = 缩到中位的一半
  - 全样本按 VOL_ratio 绝对区间分档（深度缩量 → 深度放量），输出 n / 各前瞻
    收益 / 超额（减 BTC）/ 扣成本净
  - 另按四大类（深度缩量/中等缩量/平量/放量）对 S1-S8 各场景输出 H 收益，
    检验「缩量效应」在八象限内是否一致
  - 入场 = 桶末价（无前视）；每币 ≥ MIN_BUCKETS；剔低波动

用法：
  python research_cg_vol_buckets.py --interval 12h
  python research_cg_vol_buckets.py --interval 4h --cost 0.003
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
DEFAULT_COST = 0.003

# VOL_ratio 绝对区间档（相对该币中位数；<1 缩量，>1 放量）
VOL_BUCKETS = [
    ("<0.30  深度缩量", 0.0, 0.30),
    ("0.30~0.45", 0.30, 0.45),
    ("0.45~0.60", 0.45, 0.60),
    ("0.60~0.75", 0.60, 0.75),
    ("0.75~0.90", 0.75, 0.90),
    ("0.90~1.10  平量", 0.90, 1.10),
    ("1.10~1.40", 1.10, 1.40),
    ("1.40~2.00", 1.40, 2.00),
    ("2.00~3.00", 2.00, 3.00),
    (">3.00  深度放量", 3.00, float("inf")),
]

# 四大类（S1-S8 各场景核对）
VOL_CLASSES = [
    ("深度缩量 <0.60", lambda v: v < 0.60),
    ("中等缩量 0.60~0.90", lambda v: 0.60 <= v < 0.90),
    ("平量 0.90~1.10", lambda v: 0.90 <= v < 1.10),
    ("放量 >1.10", lambda v: v >= 1.10),
]

SCENE_MAP = {
    (1, 1, 1): "S1", (1, 1, 0): "S2", (0, 1, 0): "S3", (0, 1, 1): "S4",
    (1, 0, 1): "S5", (1, 0, 0): "S6", (0, 0, 0): "S7", (0, 0, 1): "S8",
}


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

    oi_dir: dict[tuple, int] = {}
    for sym, ts, o, c in oi_rows:
        o, c = float(o), float(c)
        if o > 0:
            oi_dir[(sym, ts)] = 1 if c / o - 1.0 > 0 else 0
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
    for (sym, ts) in oi_dir:
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
            if ts not in idx or (sym, ts) not in oi_dir or (sym, ts) not in cvd_net:
                continue
            if idx[ts] + iv_h + max(HORIZONS[interval]) >= n:
                continue
            i_entry = idx[ts] + iv_h
            entry_px = px[hours[i_entry]]
            if entry_px <= 0:
                continue
            p_dir = 1 if entry_px / px[hours[idx[ts]]] - 1.0 > 0 else 0
            c_dir = 1 if cvd_net[(sym, ts)] > 0 else 0
            fwd: dict = {}
            for h in HORIZONS[interval]:
                fwd[h] = px[hours[i_entry + h]] / entry_px - 1.0
            panel.append({
                "symbol": sym, "bucket": ts,
                "p": p_dir, "oi": oi_dir[(sym, ts)], "cvd": c_dir,
                "vr": cvd_vol[(sym, ts)] / vm,     # 量能比（相对中位数）
                "fwd": fwd, "btc_fwd": btc_fwd.get(hours[i_entry], {}),
            })
    return panel


def _ex(r: dict, h: int) -> float | None:
    b = r["fwd"].get(h)
    btc = r["btc_fwd"].get(h)
    if b is None or btc is None:
        return None
    return b - btc


def stats_of(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    mean = sum(rets) / len(rets)
    win = sum(1 for x in rets if x > 0) / len(rets)
    return {"n": len(rets), "mean": mean, "win": win}


def main() -> None:
    ap = argparse.ArgumentParser(description="VOL 量能强度分档回测")
    ap.add_argument("--interval", choices=("4h", "12h"), default="12h")
    ap.add_argument("--cost", type=float, default=DEFAULT_COST)
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
    print(f"粒度={args.interval} | 面板 {len(panel)} 行 / {len({r['symbol'] for r in panel})} 币")
    hs = HORIZONS[args.interval]
    hlast = hs[-1]

    # ── 1) 全样本按 VOL_ratio 绝对区间分档 ──
    print(f"\n=== 全样本 VOL 量能强度分档（{args.interval}） ===")
    print(f"  {'档':<16} {'n':>7} | " + " | ".join(f"H{h}h" for h in hs))
    for label, lo_, hi_ in VOL_BUCKETS:
        rows = [r for r in panel if lo_ <= r["vr"] < hi_]
        if len(rows) < MIN_N:
            print(f"  {label:<16} {len(rows):>7} | 样本不足")
            continue
        cells = []
        for h in hs:
            st = stats_of([_ex(r, h) for r in rows if _ex(r, h) is not None])
            cells.append(f"{st['mean']*100:+6.2f}/{st['win']*100:3.0f}%"
                         if st["n"] >= MIN_N else "  --/--")
        print(f"  {label:<16} {len(rows):>7} | " + " | ".join(cells))

    print(f"\n  扣成本净收益（往返 {args.cost*100:.2f}%，H{hlast}h 超额）:")
    print(f"  {'档':<16} {'超额均值':>8} {'-成本净':>8}  说明")
    for label, lo_, hi_ in VOL_BUCKETS:
        rows = [r for r in panel if lo_ <= r["vr"] < hi_]
        if len(rows) < MIN_N:
            continue
        ex = statistics.mean([_ex(r, hlast) for r in rows if _ex(r, hlast) is not None])
        net = ex - args.cost
        flag = "✅ 覆盖成本" if net > 0 else ""
        print(f"  {label:<16} {ex*100:+7.2f}% {net*100:+7.2f}%  {flag}")

    # ── 2) S1-S8 × VOL 四大类 ──
    print(f"\n=== S1-S8 × VOL 四大类（H{hlast}h 超额均值 %） ===")
    print(f"  {'场景':<4} " + " | ".join(f"{nm[:8]:<8}" for nm, _ in VOL_CLASSES))
    for k in [f"S{i}" for i in range(1, 9)]:
        vals = []
        for cls, pred in VOL_CLASSES:
            rows = [r for r in panel if scene_key(r) == k and pred(r["vr"])]
            if len(rows) < MIN_N:
                vals.append("  --")
            else:
                ex = statistics.mean([_ex(r, hlast) for r in rows if _ex(r, hlast) is not None])
                vals.append(f"{ex*100:+6.2f}")
        print(f"  {k:<4} " + " | ".join(vals))
    base = []
    for cls, pred in VOL_CLASSES:
        rows = [r for r in panel if pred(r["vr"])]
        ex = statistics.mean([_ex(r, hlast) for r in rows if _ex(r, hlast) is not None]) if len(rows) >= MIN_N else float("nan")
        base.append(f"{ex*100:+6.2f}" if not math.isnan(ex) else "  --")
    print(f"  基线  " + " | ".join(base))

    print("\n完成。")


def scene_key(r: dict) -> str:
    return SCENE_MAP.get((r["p"], r["oi"], r["cvd"]), "S?")


if __name__ == "__main__":
    main()
