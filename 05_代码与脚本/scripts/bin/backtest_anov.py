#!/usr/bin/env python3
"""AnoV（异常变化强度）因子 × A/B 信号回测 — 快速版（2026 子集 + quote_vol 近似）。

AnoV 定义（f:\代码\select-coin-2\factors\AnoV.py）：
  vabs = |pct_change(2*quote_volume − taker_buy_quote_asset_volume)|
  因子  = rolling(n).apply(top 30% 均值)   # 量变化率的「爆发强度」尾部均值
⚠️ 近似：asset_klines 无 taker_buy 列，用 |pct_change(quote_vol)| 替代（因子有 abs()，
   方向信息已抹去，近似在「爆发强度」层面成立）
口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - 窗口：事件 bar 前一小时往前 n 根 1h（不含信号 bar，避免信号自身量能污染）
  - 出场：A/B 用 TRAIL3；成本 0.3%
  - 快速版：仅 2026 年事件（全量 1h K线拉取需 30min+，先验方向）

用法：python bin/backtest_anov.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from backtest_trade_params import _returns_vec, _vec_rows, load_events  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

TR = 0.03
COST = 0.003
MIN_N = 30
N_LIST = [12, 20, 48]

SQL_K = """
SELECT symbol,
       array_agg(open_time ORDER BY open_time),
       array_agg(quote_vol  ORDER BY open_time)
FROM biz.asset_klines
WHERE interval='1h' AND quote_vol > 0 AND open_time >= %s AND symbol = ANY(%s)
GROUP BY symbol
"""


def _top30_mean(arr):
    m = arr.size
    k = max(1, int(np.ceil(m * 0.3)))
    part = np.partition(arr, m - k)
    return float(np.mean(part[m - k:]))


def _load_vols(syms, start) -> dict[str, pd.Series]:
    out = {}
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), 200):
                cur.execute(SQL_K, (start, syms[k:k + 200]))
                for sym, ts, vs in cur.fetchall():
                    out[sym] = pd.Series([float(v) for v in vs],
                                         index=pd.to_datetime(list(ts)))
    return out


def _anov(vol: pd.Series, n: int) -> pd.Series:
    vabs = vol.pct_change().abs()
    # 向量化近似（原 rolling.apply(Python) 在全量 1900 万行上极慢）：
    # 前 30% 分位均值 ≈ 85% 分位数（pandas rolling.quantile 为 C 实现，秒级）
    return vabs.rolling(n, min_periods=3).quantile(0.85)


def _stats(ret):
    r = ret[~np.isnan(ret)]
    if len(r) < MIN_N:
        return None
    rn = r - COST
    win = rn > 0
    gw, gl = float(rn[win].sum()), abs(float(rn[~win].sum())) if (~win).any() else 0.0
    return {"n": len(r), "win": float((rn > 0).mean()), "mean": float(rn.mean()),
            "median": float(np.median(rn)), "pf": gw / gl if gl > 0 else float("inf")}


def _pct_rank(v, mask):
    sub = v[mask]
    out = np.full(len(v), np.nan)
    if len(sub) > 1:
        out[mask] = sub.argsort().argsort() / max(len(sub) - 1, 1)
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01",
                    help="事件与 1h 数据起始日（默认全量 2023-01-01；快速版可给更晚日期）")
    args = ap.parse_args()
    start = args.start
    rows = load_events()
    rows = [r for r in rows if r["eo"] >= pd.Timestamp(start, tz="UTC")]
    print(f"事件子集: {len(rows):,}（{start} 起）")
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1

    syms = sorted({r["symbol"] for r in rows})
    print(f"加载 1h quote_vol（{len(syms)} 币，{start} 起）...", flush=True)
    vols = _load_vols(syms, start)

    ano = {n: np.full(len(rows), np.nan) for n in N_LIST}
    miss = 0
    for i, r in enumerate(rows):
        s = vols.get(r["symbol"])
        if s is None:
            miss += 1
            continue
        idx = s.index
        t = r["eo"]
        # 事件 bar 前一小时往前 n+2 根（不含信号 bar）
        end = idx.searchsorted(t, side="left")
        if end < 3:
            continue
        win = s.iloc[max(0, end - 50):end]  # 最多取前 50 根足够 n<=48
        if len(win) < 3:
            continue
        for n in N_LIST:
            v = _anov(win, n).iloc[-1]
            if v == v:
                ano[n][i] = v
    have = ~np.isnan(ano[20])
    print(f"AnoV 可关联: {int(have.sum()):,}（缺失 {miss}）\n")

    print("=" * 108)
    print("AnoV × A/B 信号分桶（quote_vol 近似，TRAIL3，含 0.3% 成本）")
    print("=" * 108)
    for n in N_LIST:
        v = ano[n]
        have_c = ~np.isnan(v)
        print(f"\n■ AnoV n={n}（1h 窗口 {n}h）")
        for sig, m, ret in (
            ("A 做多(≥50%)", chg >= 0.50, ret_ab),
            ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50), ret_ab),
        ):
            base = _stats(np.where(m & have_c, ret, np.nan))
            print(f"  ◆ {sig}: 全池 {base['mean']*100:+.2f}% (PF {base['pf']:.1f}, n={base['n']:,})")
            rk = _pct_rank(v, m & have_c)
            print("    五档 rank:")
            for i in range(5):
                lo, hi = i / 5, (i + 1) / 5
                st = _stats(np.where(m & have_c & (rk >= lo) & (rk < hi), ret, np.nan))
                if st:
                    print(f"      Q{i+1} n={st['n']:>5,} 期望={st['mean']*100:>+6.2f}% PF={st['pf']:>5.1f}")
            print("    过滤规则:")
            for gname, f in (
                ("AnoV 高(前30%)", rk >= 0.7),
                ("AnoV 低(后30%)", rk < 0.3),
                ("AnoV 中(40~70%)", (rk >= 0.4) & (rk < 0.7)),
            ):
                st = _stats(np.where(m & have_c & f, ret, np.nan))
                if st:
                    lift = (st["mean"] - base["mean"]) * 100
                    print(f"      {gname:<16} n={st['n']:>5,} 期望={st['mean']*100:>+6.2f}% "
                          f"PF={st['pf']:>5.1f} ↑{lift:+.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
