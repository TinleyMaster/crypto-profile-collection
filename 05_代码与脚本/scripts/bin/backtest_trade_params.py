#!/usr/bin/env python3
"""交易系统参数回测：持仓时间 × 固定止盈 × 固定止损 × 杠杆（2023~2026，529 合约 1h）。

对 v2 方案的每个信号集，网格搜索最优（持仓时间 N、止盈 TP、止损 SL、杠杆 Lev）：
  - 持仓时间 N ∈ {6,12,24,36,48,72,96,120,144,168} 小时（T+6h ~ T+7d）
  - 止盈 TP ∈ {0, 5%,10%,20%,30%,50%}（0=不设）
  - 止损 SL ∈ {0, 2%,3%,5%,8%,10%}（0=不设）
  - 杠杆 Lev ∈ {1x,3x,5x,8x,10x}（净值模拟，含爆仓：单笔 -100%）

信号集（事件 = 放量大阳 bar：1h 单根≥3% & 量比≥2×；chg24=事件时点滚动 24h 涨幅，对齐线上 ticker/24hr）：
  A 短线做多   chg24≥50%
  B 次档做多   chg24 20~50%
  C 诱多做空   chg24<5%（方向做空）
  D 极端做空   chg24≥75%（方向做空）

出场规则（币安条件单 STOP_MARKET/TAKE_PROFIT_MARKET 同构）：
  先触止盈 → +TP；先触止损 → -SL；同 bar 同时触 → 保守算止损；持有 N 小时未触 → 按第 N 根收盘平仓。

服务端一次性计算每事件的「首次触达序号 + 各时点收盘」，本地 numpy 向量化跑网格。
用法：python bin/backtest_trade_params.py [--signal A] [--leverage 1 3 5 8 10] [--limit 200000]
"""
from __future__ import annotations

import argparse
import csv
import statistics
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
CACHE = DATA_DIR / "trade_params_events.csv"   # SQL 中间结果缓存

N_LIST = [6, 12, 24, 36, 48, 72, 96, 120, 144, 168]
TP_LIST = [0.0, 0.05, 0.10, 0.20, 0.30, 0.50]
SL_LIST = [0.0, 0.03, 0.05, 0.08, 0.10]      # 止损档（10% 已满足 -15% 风险约束）
TR_LIST = [0.03, 0.05, 0.08, 0.10, 0.15]            # 跟踪止盈（峰值回撤），仅做多
LEV_LIST = [1, 3, 5, 8, 10]
POS_PCT = 0.30          # 净值模拟：单笔投入占权益比例（100U 实验 ≈ 30U/笔）
MIN_N = 30
WORST_LIMIT = -0.15     # 选择约束：单笔最差收益 ≥ -15%（防黑天鹅）

SQL = """
WITH volbase AS (
    SELECT symbol, open_time, close_px,
           close_px / open_px - 1 AS chg1h,
           quote_vol,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0
),
evt AS (
    SELECT symbol, open_time, close_px AS entry, chg1h,
           quote_vol / avg20 AS vr
    FROM volbase
    WHERE avg20 > 0 AND quote_vol / avg20 >= 1.5 AND abs(chg1h) >= 0.015
),
dchg AS (
    -- 事件时点可得的滚动 24h 涨幅：事件 bar 收盘 / 24h 前（含）那根 bar 收盘。
    -- 对齐线上 ticker/24hr，严禁使用当日日终收盘（否则引入前视偏差）。
    SELECT DISTINCT ON (e.symbol, e.open_time)
           e.symbol, e.open_time, e.entry / p.close_px - 1 AS chg24
    FROM evt e
    JOIN biz.asset_klines p
      ON p.symbol = e.symbol AND p.interval = '1h' AND p.close_px > 0
     AND p.open_time <= e.open_time - interval '24 hours'
     AND p.open_time >  e.open_time - interval '36 hours'   -- 容忍缺 bar，回看上限 12h
    ORDER BY e.symbol, e.open_time, p.open_time DESC
),
ev AS (
    SELECT e.symbol, e.open_time AS eo, e.entry, e.chg1h, e.vr, d.chg24
    FROM evt e
    JOIN dchg d ON d.symbol = e.symbol AND d.open_time = e.open_time
    WHERE e.chg1h >= 0.03 AND e.vr >= 2
),
bars AS (
    SELECT ev.symbol, ev.eo, ev.entry, ev.chg1h, ev.vr, ev.chg24,
           k.high_px, k.low_px, k.close_px,
           (EXTRACT(EPOCH FROM (k.open_time - ev.eo)) / 3600)::int AS h_off
    FROM ev
    JOIN biz.asset_klines k ON k.symbol = ev.symbol AND k.interval = '1h'
         AND k.open_time > ev.eo AND k.open_time <= ev.eo + interval '168 hours'
         AND k.high_px > 0 AND k.low_px > 0 AND k.close_px > 0
),
bq AS MATERIALIZED (
    SELECT b.*,
           MAX(high_px) OVER (PARTITION BY symbol, eo ORDER BY h_off
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS ph
    FROM bars b
),
agg AS (
    SELECT symbol, eo, entry, chg1h, vr, chg24,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.02) AS hp2,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.03) AS hp3,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.05) AS hp5,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.08) AS hp8,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.10) AS hp10,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.20) AS hp20,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.30) AS hp30,
       MIN(h_off) FILTER (WHERE high_px >= entry * 1.50) AS hp50,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.98) AS sl2,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.97) AS sl3,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.95) AS sl5,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.92) AS sl8,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.90) AS sl10,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.80) AS sl20,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.70) AS sl30,
       MIN(h_off) FILTER (WHERE low_px <= entry * 0.50) AS sl50,
       MAX(close_px) FILTER (WHERE h_off = 6)   AS c6,
       MAX(close_px) FILTER (WHERE h_off = 12)  AS c12,
       MAX(close_px) FILTER (WHERE h_off = 24)  AS c24,
       MAX(close_px) FILTER (WHERE h_off = 36)  AS c36,
       MAX(close_px) FILTER (WHERE h_off = 48)  AS c48,
       MAX(close_px) FILTER (WHERE h_off = 72)  AS c72,
       MAX(close_px) FILTER (WHERE h_off = 96)  AS c96,
       MAX(close_px) FILTER (WHERE h_off = 120) AS c120,
       MAX(close_px) FILTER (WHERE h_off = 144) AS c144,
       MAX(close_px) FILTER (WHERE h_off = 168) AS c168,
       -- 跟踪止盈：入场后滚动最高点回撤 X% 的首次触达序号（不含入场 bar）
       MIN(h_off) FILTER (WHERE low_px <= ph * 0.97)  AS tr3,
       MIN(h_off) FILTER (WHERE low_px <= ph * 0.95)  AS tr5,
       MIN(h_off) FILTER (WHERE low_px <= ph * 0.92)  AS tr8,
       MIN(h_off) FILTER (WHERE low_px <= ph * 0.90)  AS tr10,
       MIN(h_off) FILTER (WHERE low_px <= ph * 0.85)  AS tr15,
       MAX(ph) AS px_peak   -- 168h 窗口内滚动最高（仅用于参考，不出场计算）
    FROM bq
    GROUP BY symbol, eo, entry, chg1h, vr, chg24
),
-- 跟踪止盈触发时刻的滚动峰值（出场价 = 触发时刻峰值 ×(1-tr)）
tr3_first AS (
    SELECT DISTINCT ON (symbol, eo) symbol, eo, ph AS ph_tr3
    FROM bq WHERE low_px <= ph * 0.97 ORDER BY symbol, eo, h_off
),
tr5_first AS (
    SELECT DISTINCT ON (symbol, eo) symbol, eo, ph AS ph_tr5
    FROM bq WHERE low_px <= ph * 0.95 ORDER BY symbol, eo, h_off
),
tr8_first AS (
    SELECT DISTINCT ON (symbol, eo) symbol, eo, ph AS ph_tr8
    FROM bq WHERE low_px <= ph * 0.92 ORDER BY symbol, eo, h_off
),
tr10_first AS (
    SELECT DISTINCT ON (symbol, eo) symbol, eo, ph AS ph_tr10
    FROM bq WHERE low_px <= ph * 0.90 ORDER BY symbol, eo, h_off
),
tr15_first AS (
    SELECT DISTINCT ON (symbol, eo) symbol, eo, ph AS ph_tr15
    FROM bq WHERE low_px <= ph * 0.85 ORDER BY symbol, eo, h_off
)
SELECT a.*,
       t3.ph_tr3, t5.ph_tr5, t8.ph_tr8, t10.ph_tr10, t15.ph_tr15
FROM agg a
LEFT JOIN tr3_first  t3  ON t3.symbol  = a.symbol AND t3.eo  = a.eo
LEFT JOIN tr5_first  t5  ON t5.symbol  = a.symbol AND t5.eo  = a.eo
LEFT JOIN tr8_first  t8  ON t8.symbol  = a.symbol AND t8.eo  = a.eo
LEFT JOIN tr10_first t10 ON t10.symbol = a.symbol AND t10.eo = a.eo
LEFT JOIN tr15_first t15 ON t15.symbol = a.symbol AND t15.eo = a.eo
"""

# 列名 ↔ 阈值（high 触达=做多止盈/做空止损；low 触达=做多止损/做空止盈）
HP_COLS = [("hp2", 0.02), ("hp3", 0.03), ("hp5", 0.05), ("hp8", 0.08),
           ("hp10", 0.10), ("hp20", 0.20), ("hp30", 0.30), ("hp50", 0.50)]
SL_COLS = [("sl2", 0.02), ("sl3", 0.03), ("sl5", 0.05), ("sl8", 0.08), ("sl10", 0.10),
           ("sl20", 0.20), ("sl30", 0.30), ("sl50", 0.50)]
# 跟踪止盈：做多用低点回撤（low ≤ 滚动最高×(1-X)）；做空用高点回撤（high ≥ 滚动最低×(1+X)）
TR_COLS = [("tr3", 0.03), ("tr5", 0.05), ("tr8", 0.08), ("tr10", 0.10), ("tr15", 0.15)]
C_COLS = [(n, v) for n, v in zip(
    ["c6", "c12", "c24", "c36", "c48", "c72", "c96", "c120", "c144", "c168"], N_LIST)]

SIGNALS = {
    "A": ("短线做多 chg24≥50%", lambda r: r["chg24"] >= 0.50, 1),      # 方向 +1 做多
    "B": ("次档做多 chg24 20~50%", lambda r: 0.20 <= r["chg24"] < 0.50, 1),
    "C": ("诱多做空 chg24<5%", lambda r: r["chg24"] < 0.05, -1),        # 方向 -1 做空
    "D": ("极端做空 chg24≥75%", lambda r: r["chg24"] >= 0.75, -1),
}


def load_events() -> list[dict]:
    if CACHE.exists():
        with CACHE.open(newline="") as f:
            rd = csv.DictReader(f)
            rows = [dict(r) for r in rd]
        for r in rows:
            r["eo"] = datetime.fromisoformat(r["eo"])
            for k in list(rows[0].keys()):
                if k in ("symbol", "eo"):
                    continue
                v = r[k]
                r[k] = None if v in (None, "") else float(v)
        return rows

    settings = get_settings(require_database=True)
    # COPY 流式落盘缓存。远程网络下 COPY 结束握手可能永久卡住（数据已完整写盘）——
    # 故 COPY 放 daemon 线程执行，主线程监控文件停止增长 180s 即判定完成并读回。
    import threading
    import time

    conn = psycopg.connect(settings.database_url, connect_timeout=20)

    def _writer():
        try:
            with conn.cursor() as cur:
                with cur.copy(f"COPY ({SQL}) TO STDOUT WITH (FORMAT csv, HEADER true)") as cp:
                    with CACHE.open("wb") as f:
                        while (chunk := cp.read()):
                            f.write(chunk)
        except Exception:
            pass

    threading.Thread(target=_writer, daemon=True).start()
    prev = -1
    stall = 0
    while True:
        if not CACHE.exists() or CACHE.stat().st_size == 0:
            time.sleep(15)          # SQL 计算阶段文件未出现，继续等
            continue
        size = CACHE.stat().st_size
        if size == prev:
            stall += 1
            if stall >= 12:         # 12 × 15s = 180s 无增长 → 判定完成
                break
        else:
            stall = 0
            prev = size
        time.sleep(15)
    with CACHE.open(newline="") as f:
        rd = csv.DictReader(f)
        rows = [dict(r) for r in rd]
    for r in rows:
        r["eo"] = datetime.fromisoformat(r["eo"])
        for k in r:
            if k in ("symbol", "eo"):
                continue
            v = r[k]
            r[k] = None if v in (None, "") else float(v)
    return rows


def _vec_rows(rows):
    """把事件列表转成 numpy 结构化数组（NaN=缺失），供向量化网格。"""
    TR_PH_COLS = ["ph_tr3", "ph_tr5", "ph_tr8", "ph_tr10", "ph_tr15"]
    keys = (["entry", "chg24", "eo", "px_peak"] + [c for c, _ in HP_COLS]
            + [c for c, _ in SL_COLS] + [c for c, _ in C_COLS] + [c for c, _ in TR_COLS]
            + TR_PH_COLS)
    out: dict[str, np.ndarray] = {}
    for k in keys:
        if k == "eo":
            out[k] = np.asarray([r[k].timestamp() for r in rows], dtype=float)  # r[k] 已是 datetime
        else:
            out[k] = np.asarray([np.nan if r.get(k) is None else float(r[k]) for r in rows],
                                dtype=float)
    return out


def _returns_vec(arr, direction, n, tp, sl, tr):
    """向量化单笔毛收益。模式：
      - 固定（tr=None）：先触固定止盈 tp / 固定止损 sl / 时间到期
      - 跟踪（tp=None，仅做多）：先触跟踪止盈 tr / 固定止损 sl / 时间到期
        跟踪止盈出场收益 = 峰值×(1-tr)/entry - 1（用 168h 全窗口峰值近似，略偏乐观）
    做空不启用跟踪（SQL 未算做空侧滚动最低回撤）。"""
    entry = arr["entry"]
    cn = arr[f"c{n}"]
    valid = ~np.isnan(cn)
    if direction == 1:      # 做多
        if tr is not None:
            t_tp = arr[f"tr{int(tr*100)}"]
        else:
            t_tp = np.full_like(entry, np.nan) if tp == 0 else arr[f"hp{int(tp*100)}"]
        t_sl = np.full_like(entry, np.nan) if sl == 0 else arr[f"sl{int(sl*100)}"]
    else:                   # 做空
        t_tp = np.full_like(entry, np.nan) if tp == 0 else arr[f"sl{int(tp*100)}"]
        t_sl = np.full_like(entry, np.nan) if sl == 0 else arr[f"hp{int(sl*100)}"]
    t_tp = np.where(t_tp <= n, t_tp, np.nan)
    t_sl = np.where(t_sl <= n, t_sl, np.nan)
    ret_time = direction * (cn / entry - 1)
    hit_tp = ~np.isnan(t_tp) & (np.isnan(t_sl) | (t_tp < t_sl))
    hit_sl = ~np.isnan(t_sl) & (np.isnan(t_tp) | (t_sl <= t_tp))
    if tr is not None:      # 跟踪止盈出场收益：触发时刻的滚动峰值 ×(1-tr)
        ph_at_tr = arr[f"ph_tr{int(tr * 100)}"]
        tp_val = ph_at_tr * (1 - tr) / entry - 1
    else:
        tp_val = tp
    ret = np.where(hit_tp, tp_val, np.where(hit_sl, -sl, ret_time))
    ret = np.where(valid, ret, np.nan)
    return ret


def grid_signal(arr, direction, lev_list=LEV_LIST):
    """对一组事件做网格，返回统计列表（固定/跟踪两种出场模式，单笔统计 + 风险约束）。"""
    results = []
    for n in N_LIST:
        cn = arr[f"c{n}"]
        if np.isnan(cn).sum() > 0 and len(cn) - np.isnan(cn).sum() < MIN_N:
            continue
        for sl in SL_LIST:
            for tp in TP_LIST:      # 固定止盈
                ret = _returns_vec(arr, direction, n, tp, sl, None)
                rs = ret[~np.isnan(ret)]
                if len(rs) < MIN_N:
                    continue
                results.append({"mode": "FIX", "N": n, "TP": tp, "TR": None, "SL": sl,
                                "n": len(rs), "win": float((rs > 0).mean()),
                                "mean": float(rs.mean()), "med": float(np.median(rs)),
                                "pf": float((rs[rs > 0].sum() + 1e-12) /
                                            (abs(rs[rs <= 0].sum()) + 1e-12)),
                                "worst": float(np.nanmin(rs))})
            if direction == 1:      # 跟踪止盈（仅做多）
                for tr in TR_LIST:
                    ret = _returns_vec(arr, direction, n, 0, sl, tr)
                    rs = ret[~np.isnan(ret)]
                    if len(rs) < MIN_N:
                        continue
                    results.append({"mode": "TRAIL", "N": n, "TP": None, "TR": tr, "SL": sl,
                                    "n": len(rs), "win": float((rs > 0).mean()),
                                    "mean": float(rs.mean()), "med": float(np.median(rs)),
                                    "pf": float((rs[rs > 0].sum() + 1e-12) /
                                                (abs(rs[rs <= 0].sum()) + 1e-12)),
                                    "worst": float(np.nanmin(rs))})
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="ALL", choices=["ALL", *SIGNALS])
    ap.add_argument("--leverage", nargs="+", type=int, default=LEV_LIST)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = load_events()
    print(f"[events] {len(rows):,} 条（涨异动×滚动24h口径）；缓存 {CACHE}")
    if args.limit:
        rows = rows[: args.limit]

    out_csv = DATA_DIR / "backtest_trade_params.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signal", "label", "mode", "N_h", "TP_pct", "TR_pct", "SL_pct",
                    "n", "win", "mean", "med", "pf", "worst_pct"])
        for key in (SIGNALS if args.signal == "ALL" else [args.signal]):
            label, cond, direction = SIGNALS[key]
            sub = [r for r in rows if cond(r)]
            print(f"\n== {key} {label} ==")
            print(f"  事件数 {len(sub)}")
            if len(sub) < MIN_N:
                continue
            arr = _vec_rows(sub)
            grid = grid_signal(arr, direction, args.leverage)
            # 选择标准：单笔最差 ≥ WORST_LIMIT（风险约束）→ PF 降序 → 胜率降序
            safe = [g for g in grid if g["worst"] >= WORST_LIMIT]
            pool = safe if len(safe) >= 3 else grid
            pool.sort(key=lambda g: (g["pf"], g["win"]), reverse=True)
            print(f"{'模式':>5} {'N(h)':>5} {'TP%':>5} {'TR%':>5} {'SL%':>5} | "
                  f"{'n':>6} {'胜%':>6} {'期望%':>8} {'中位%':>7} {'PF':>6} {'最差%':>7}")
            for g in pool[:14]:
                mode = "FIX" if g["mode"] == "FIX" else "TRL"
                tp_s = f"{g['TP']*100:.0f}" if g["TP"] is not None else "-"
                tr_s = f"{g['TR']*100:.0f}" if g["TR"] is not None else "-"
                print(f"{mode:>5} {g['N']:>5} {tp_s:>5} {tr_s:>5} {g['SL']*100:>5.0f} | "
                      f"{g['n']:>6,} {g['win']*100:>6.1f} {g['mean']*100:>8.2f} "
                      f"{g['med']*100:>7.2f} {g['pf']:>6.2f} {g['worst']*100:>7.1f}")
                w.writerow([key, label, g["mode"], g["N"],
                            "" if g["TP"] is None else g["TP"] * 100,
                            "" if g["TR"] is None else g["TR"] * 100,
                            g["SL"] * 100, g["n"], round(g["win"], 4), round(g["mean"], 4),
                            round(g["med"], 4), round(g["pf"], 4), round(g["worst"], 4)])
            if pool:
                best = pool[0]
                print(f"\n  ★ 最优（风险约束 + PF）: {best['mode']} N={best['N']}h "
                      f"TP={best['TP']} TR={best['TR']} SL={best['SL']*100:.0f}% "
                      f"| 胜 {best['win']*100:.1f}% 期望 {best['mean']*100:.2f}% "
                      f"中位 {best['med']*100:.2f}% PF {best['pf']:.2f} 最差 {best['worst']*100:.1f}%")
    print(f"\n结果已存 {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
