#!/usr/bin/env python3
"""UpRatio 因子 × A/B/C 信号回测。

UpRatio = A/(A+B)，A=N周期上涨幅度和，B=N周期下跌幅度绝对值
  范围[0,1]：高=近N周期上涨贡献主导（干净上涨趋势），低=涨跌来回（震荡）
n=10/20/30

口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - UpRatio：日线 close.diff() 滚动窗口；事件取**前一交易日**（无前视）
  - 出场：A/B 用 TRAIL3；C 用 FIX12h；成本 0.3%
  - ① UpRatio 五档；② 过滤规则扫描

用法：python bin/backtest_up_ratio.py
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

from backtest_trade_params import CACHE, _returns_vec, _vec_rows, load_events  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

TR = 0.03
COST = 0.003
MIN_N = 50
N_LIST = [10, 20, 30]

SQL_DAILY = """
SELECT DISTINCT ON (symbol, DATE(open_time)) symbol, DATE(open_time) AS d, close_px
FROM biz.asset_klines
WHERE interval = '1h' AND close_px > 0
  AND open_time >= '2022-12-01' AND symbol = ANY(%s)
ORDER BY symbol, DATE(open_time), open_time DESC
"""

GATES = [
    ("全池（不过滤）", lambda u, n: np.ones_like(u, dtype=bool)),
    ("只留 UpRatio≥0.7", lambda u, n: u >= 0.7),
    ("只留 UpRatio≥0.8", lambda u, n: u >= 0.8),
    ("只留 UpRatio≥0.9", lambda u, n: u >= 0.9),
    ("只留 UpRatio<0.5", lambda u, n: u < 0.5),
    ("只留 0.5~0.7", lambda u, n: (u >= 0.5) & (u < 0.7)),
]


def _load_daily(syms) -> dict[str, pd.DataFrame]:
    dailies = []
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), 50):
                cur.execute(SQL_DAILY, (syms[k:k + 50],))
                dailies.extend(cur.fetchall())
    per = defaultdict(list)
    for sym, d, close in dailies:
        per[sym].append((d, float(close)))
    out = {}
    for sym, pairs in per.items():
        pairs.sort()
        df = pd.DataFrame(pairs, columns=["d", "close"]).set_index("d")
        diff = df["close"].diff().fillna(0.0)
        up = diff.clip(lower=0)
        down = (-diff).clip(lower=0)
        for n in N_LIST:
            a = up.rolling(n, min_periods=1).sum()
            b = down.rolling(n, min_periods=1).sum()
            ur = a / (a + b)
            df[f"ur{n}"] = ur.replace([np.inf, -np.inf], np.nan).fillna(0.5)
        out[sym] = df
    return out


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
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    ret_c = _returns_vec(arr, -1, 12, 0.50, 0.10, None)

    syms = sorted({r["symbol"] for r in rows})
    print("加载日线算 UpRatio...", flush=True)
    daily = _load_daily(syms)

    ur = {n: np.full(len(rows), np.nan) for n in N_LIST}
    for i, r in enumerate(rows):
        df = daily.get(r["symbol"])
        if df is None:
            continue
        d = r["eo"].date() - timedelta(days=1)
        if d in df.index:
            for n in N_LIST:
                v = float(df.loc[d, f"ur{n}"])
                if v == v:
                    ur[n][i] = v
    have = ~np.isnan(ur[20])
    print(f"UpRatio 可关联事件: {int(have.sum()):,} / {len(rows):,}\n")

    for n in N_LIST:
        u = ur[n]
        have_c = ~np.isnan(u)
        print("=" * 108)
        print(f"■ UpRatio n={n}：A/B/C 五档 + 过滤规则")
        print("=" * 108)
        for sig, m, ret in (
            ("A 做多(≥50%)", chg >= 0.50, ret_ab),
            ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50), ret_ab),
            ("C 做空(<5%)", chg < 0.05, ret_c),
        ):
            base = _stats(np.where(m & have_c, ret, np.nan))
            print(f"\n◆ {sig}: 全池 {base['mean']*100:+.2f}% (PF {base['pf']:.1f}, n={base['n']:,})")
            rk = _pct_rank(u, m & have_c)
            print("  五档 rank:")
            for i in range(5):
                lo, hi = i / 5, (i + 1) / 5
                st = _stats(np.where(m & have_c & (rk >= lo) & (rk < hi), ret, np.nan))
                if st:
                    print(f"    Q{i+1} n={st['n']:>6,} 期望={st['mean']*100:>+7.2f}% "
                          f"胜率={st['win']*100:>5.1f}% PF={st['pf']:>5.1f}")
            print("  过滤规则:")
            best = []
            for gname, f in GATES[1:]:
                st = _stats(np.where(m & have_c & f(u, n), ret, np.nan))
                if st:
                    lift = (st["mean"] - base["mean"]) * 100
                    print(f"    {gname:<20} n={st['n']:>6,} 期望={st['mean']*100:>+7.2f}% "
                          f"PF={st['pf']:>5.1f} ↑{lift:+.2f}pp")
                    if st["n"] >= 100:
                        best.append((st["mean"], gname, st))
            if sig.startswith("A"):
                best.sort(reverse=True)
                if best:
                    m_, g_, s_ = best[0]
                    print(f"    ★ A 最优过滤: {g_} → {m_*100:+.2f}% (PF {s_['pf']:.1f}, n={s_['n']:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
