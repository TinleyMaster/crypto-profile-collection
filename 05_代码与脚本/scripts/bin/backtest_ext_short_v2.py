#!/usr/bin/env python3
"""EXT_SHORT（极端做空）回测 v2：放宽量比条件。

放宽方案：
  - 方案 A：量比 ≤1.0×（严格，30 样本）
  - 方案 B：量比 ≤1.5×（放宽，更多样本）
  - 方案 C：量比 ≤2.0×（最宽）

对每个方案，测试做空方向的参数网格。

用法：python bin/backtest_ext_short_v2.py
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"

# 参数网格
N_LIST = [6, 12, 24, 36, 48, 72, 96, 120, 144, 168]
TP_LIST = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
SL_LIST = [0.05, 0.08, 0.10, 0.15]
LEV_LIST = [1]
COST = 0.002
MIN_N = 20  # 放宽最低样本要求
WORST_LIMIT = -0.20  # 放宽风险约束


def build_sql(vr_max: float) -> str:
    return f"""
    -- 日线聚合：事件本体 = 当日最后一根 1h bar（entry/eo 与 chg24 同源，日线收盘粒度）
    -- day_vol = 当日全部 1h bar 的 quote_vol 之和（做量比的分子，与日均量同量纲）
    WITH daily AS (
        SELECT symbol, DATE(open_time) AS d,
               MAX(open_time) AS open_time,
               (ARRAY_AGG(close_px ORDER BY open_time DESC))[1] AS close_px,
               SUM(quote_vol) AS day_vol
        FROM biz.asset_klines
        WHERE interval = '1h' AND close_px > 0 AND open_px > 0
        GROUP BY symbol, DATE(open_time)
    ),
    -- 量比基线：事件日之前 7 个交易日的日均量（trailing 窗口，只用事件时点可得的历史，
    -- 严禁使用 NOW()/未来窗口，否则对 2021~2026 历史事件构成前视泄漏）
    vol_avg AS (
        SELECT symbol, d,
               AVG(day_vol) OVER (
                   PARTITION BY symbol ORDER BY d
                   ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
               ) AS avg_daily_vol
        FROM daily
    ),
    dchg AS (
        SELECT s.symbol, s.d, s.open_time AS eo, s.close_px AS entry,
               s.close_px / p.close_px - 1 AS chg24,
               CASE WHEN va.avg_daily_vol > 0
                    THEN s.day_vol / va.avg_daily_vol
                    ELSE NULL END AS vol_ratio
        FROM daily s
        JOIN daily p ON p.symbol = s.symbol AND p.d = s.d - 1
        LEFT JOIN vol_avg va ON va.symbol = s.symbol AND va.d = s.d
        WHERE s.close_px / p.close_px - 1 >= 0.75
    ),
    ev AS (
        SELECT symbol, eo, entry, chg24, vol_ratio
        FROM dchg
        WHERE vol_ratio IS NOT NULL AND vol_ratio <= {vr_max}
    ),
    bars AS (
        SELECT ev.symbol, ev.eo, ev.entry, ev.chg24, ev.vol_ratio,
               k.high_px, k.low_px, k.close_px,
               (EXTRACT(EPOCH FROM (k.open_time - ev.eo)) / 3600)::int AS h_off
        FROM ev
        JOIN biz.asset_klines k ON k.symbol = ev.symbol AND k.interval = '1h'
             AND k.open_time > ev.eo AND k.open_time <= ev.eo + interval '168 hours'
             AND k.high_px > 0 AND k.low_px > 0 AND k.close_px > 0
    )
    SELECT symbol, eo, entry, chg24, vol_ratio,
           -- 做空止盈（价格下跌触达）
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.95) AS tp5,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.90) AS tp10,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.85) AS tp15,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.80) AS tp20,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.75) AS tp25,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.70) AS tp30,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.60) AS tp40,
           MIN(h_off) FILTER (WHERE low_px <= entry * 0.50) AS tp50,
           -- 做空止损（价格上涨触达）
           MIN(h_off) FILTER (WHERE high_px >= entry * 1.05) AS sl5,
           MIN(h_off) FILTER (WHERE high_px >= entry * 1.08) AS sl8,
           MIN(h_off) FILTER (WHERE high_px >= entry * 1.10) AS sl10,
           MIN(h_off) FILTER (WHERE high_px >= entry * 1.15) AS sl15,
           -- 各时点收盘
           MAX(close_px) FILTER (WHERE h_off = 6) AS c6,
           MAX(close_px) FILTER (WHERE h_off = 12) AS c12,
           MAX(close_px) FILTER (WHERE h_off = 24) AS c24,
           MAX(close_px) FILTER (WHERE h_off = 36) AS c36,
           MAX(close_px) FILTER (WHERE h_off = 48) AS c48,
           MAX(close_px) FILTER (WHERE h_off = 72) AS c72,
           MAX(close_px) FILTER (WHERE h_off = 96) AS c96,
           MAX(close_px) FILTER (WHERE h_off = 120) AS c120,
           MAX(close_px) FILTER (WHERE h_off = 144) AS c144,
           MAX(close_px) FILTER (WHERE h_off = 168) AS c168
    FROM bars
    GROUP BY symbol, eo, entry, chg24, vol_ratio
    """


def fetch_events(sql: str, cache_name: str) -> list[dict]:
    cache = DATA_DIR / cache_name
    if cache.exists():
        with cache.open(newline="") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
        for r in rows:
            r["eo"] = datetime.fromisoformat(r["eo"])
            for k in list(rows[0].keys()):
                if k in ("symbol", "eo"):
                    continue
                r[k] = None if r[k] in (None, "") else float(r[k])
        return rows

    settings = get_settings(require_database=True)
    import threading, time
    conn = psycopg.connect(settings.database_url, connect_timeout=20)

    def _writer():
        try:
            with conn.cursor() as cur:
                with cur.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)") as cp:
                    with cache.open("wb") as f:
                        while chunk := cp.read():
                            f.write(chunk)
        except Exception:
            pass

    threading.Thread(target=_writer, daemon=True).start()
    prev, stall = -1, 0
    while True:
        if not cache.exists() or cache.stat().st_size == 0:
            time.sleep(10)
            continue
        size = cache.stat().st_size
        if size == prev:
            stall += 1
            if stall >= 12:
                break
        else:
            stall, prev = 0, size
        time.sleep(10)

    with cache.open(newline="") as f:
        rows = [dict(r) for r in csv.DictReader(f)]
    for r in rows:
        r["eo"] = datetime.fromisoformat(r["eo"])
        for k in r:
            if k in ("symbol", "eo"):
                continue
            r[k] = None if r[k] in (None, "") else float(r[k])
    return rows


def _vec_rows(rows):
    TP_COLS = [("tp5", 0.05), ("tp10", 0.10), ("tp15", 0.15), ("tp20", 0.20),
               ("tp25", 0.25), ("tp30", 0.30), ("tp40", 0.40), ("tp50", 0.50)]
    SL_COLS = [("sl5", 0.05), ("sl8", 0.08), ("sl10", 0.10), ("sl15", 0.15)]
    C_COLS = [(f"c{n}", n) for n in N_LIST]
    keys = ["entry", "chg24", "vol_ratio", "eo"] + \
           [c for c, _ in TP_COLS] + [c for c, _ in SL_COLS] + [c for c, _ in C_COLS]
    out = {}
    for k in keys:
        if k == "eo":
            out[k] = np.asarray([r[k].timestamp() for r in rows], dtype=float)
        else:
            out[k] = np.asarray([np.nan if r.get(k) is None else float(r[k]) for r in rows], dtype=float)
    return out


def _returns_short(arr, n, tp, sl, cost=0.0):
    entry = arr["entry"]
    cn = arr[f"c{n}"]
    valid = ~np.isnan(cn)
    t_tp = np.full_like(entry, np.nan) if tp == 0 else arr[f"tp{int(tp*100)}"]
    t_sl = np.full_like(entry, np.nan) if sl == 0 else arr[f"sl{int(sl*100)}"]
    t_tp = np.where(t_tp <= n, t_tp, np.nan)
    t_sl = np.where(t_sl <= n, t_sl, np.nan)
    ret_time = entry / cn - 1
    hit_tp = ~np.isnan(t_tp) & (np.isnan(t_sl) | (t_tp < t_sl))
    hit_sl = ~np.isnan(t_sl) & (np.isnan(t_tp) | (t_sl <= t_tp))
    ret = np.where(hit_tp, tp, np.where(hit_sl, -sl, ret_time))
    ret = np.where(valid, ret, np.nan)
    return ret - cost


def grid_search(arr, label: str):
    results = []
    for n in N_LIST:
        cn = arr[f"c{n}"]
        if np.isnan(cn).sum() > 0 and len(cn) - np.isnan(cn).sum() < MIN_N:
            continue
        for sl in SL_LIST:
            for tp in TP_LIST:
                ret = _returns_short(arr, n, tp, sl, COST)
                rs = ret[~np.isnan(ret)]
                if len(rs) < MIN_N:
                    continue
                results.append({
                    "N": n, "TP": tp, "SL": sl,
                    "n": len(rs), "win": float((rs > 0).mean()),
                    "mean": float(rs.mean()), "med": float(np.median(rs)),
                    "pf": float((rs[rs > 0].sum() + 1e-12) / (abs(rs[rs <= 0].sum()) + 1e-12)),
                    "worst": float(np.nanmin(rs)),
                })

    safe = [g for g in results if g["worst"] >= WORST_LIMIT]
    pool = safe if len(safe) >= 3 else results
    pool.sort(key=lambda g: (g["pf"], g["win"]), reverse=True)

    print(f"\n{'='*70}")
    print(f"  {label}（{len(arr['entry'])} 事件）")
    print(f"{'='*70}")
    print(f"{'N(h)':>5} {'TP%':>5} {'SL%':>5} | {'n':>5} {'胜%':>6} {'期望%':>8} {'中位%':>7} {'PF':>6} {'最差%':>7}")
    print("-" * 60)
    for g in pool[:10]:
        print(f"{g['N']:>5} {g['TP']*100:>5.0f} {g['SL']*100:>5.0f} | "
              f"{g['n']:>5} {g['win']*100:>6.1f} {g['mean']*100:>8.2f} "
              f"{g['med']*100:>7.2f} {g['pf']:>6.2f} {g['worst']*100:>7.1f}")

    if pool:
        best = pool[0]
        print(f"\n  ★ 最优: N={best['N']}h TP={best['TP']*100:.0f}% SL={best['SL']*100:.0f}%")
        print(f"    胜率 {best['win']*100:.1f}% 期望 {best['mean']*100:.2f}% PF {best['pf']:.2f} 最差 {best['worst']*100:.1f}%")

    return pool


def main() -> int:
    print("=" * 70)
    print("EXT_SHORT 做空回测（放宽量比条件对比）")
    print("=" * 70)

    for vr_max, label, cache_name in [
        (1.0, "方案 A：量比 ≤1.0×（严格）", "ext_short_v2_vr10.csv"),
        (1.5, "方案 B：量比 ≤1.5×（放宽）", "ext_short_v2_vr15.csv"),
        (2.0, "方案 C：量比 ≤2.0×（最宽）", "ext_short_v2_vr20.csv"),
    ]:
        sql = build_sql(vr_max)
        rows = fetch_events(sql, cache_name)
        arr = _vec_rows(rows)
        grid_search(arr, label)

    return 0


if __name__ == "__main__":
    sys.exit(main())