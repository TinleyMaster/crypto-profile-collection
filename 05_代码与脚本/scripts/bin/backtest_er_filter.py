#!/usr/bin/env python3
"""ER（有效路径/效率比率）因子 × A/B/C 信号回测 + B 三维排序。

ER（Kaufman Efficiency Ratio）：
  ER = |close − close.shift(n)| / Σ|close − close.shift(1)|（n 日净位移 / n 日总路径）
  范围 0~1：高=直线上涨（有效路径/趋势干净），低=来回震荡（无效路径）

口径（2026-10-07）：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - ER：日线（每天最后 1h close）算 er10/20/30；事件取**前一交易日**（无前视）
  - 出场：A/B 用 TRAIL3（ph_tr3×0.97/entry-1）；C 用 FIX 12h/TP50/SL10；成本 0.3%
  - ① ER × A/B/C 分桶；② B 三维排序（涨幅+vr+ER rank 平均），对照二维

用法：python bin/backtest_er_filter.py
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
N_ER = [10, 20, 30]

SQL_DAILY = """
SELECT DISTINCT ON (symbol, DATE(open_time)) symbol, DATE(open_time) AS d, close_px
FROM biz.asset_klines
WHERE interval = '1h' AND close_px > 0
  AND open_time >= '2022-12-01' AND symbol = ANY(%s)
ORDER BY symbol, DATE(open_time), open_time DESC
"""

ER_BINS = [
    ("ER<0.20 震荡", lambda e: e < 0.20),
    ("ER 0.20~0.40", lambda e: (0.20 <= e) & (e < 0.40)),
    ("ER 0.40~0.60", lambda e: (0.40 <= e) & (e < 0.60)),
    ("ER 0.60~0.80", lambda e: (0.60 <= e) & (e < 0.80)),
    ("ER≥0.80 直线趋势", lambda e: e >= 0.80),
]


def _load_daily(syms) -> dict[str, pd.DataFrame]:
    dailies = []
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), 50):
                cur.execute(SQL_DAILY, (syms[k:k + 50],))
                dailies.extend(cur.fetchall())
    per: dict[str, list] = defaultdict(list)
    for sym, d, close in dailies:
        per[sym].append((d, float(close)))
    out = {}
    for sym, pairs in per.items():
        pairs.sort()
        df = pd.DataFrame(pairs, columns=["d", "close"]).set_index("d")
        c = df["close"]
        d1 = c.diff().abs()
        for n in N_ER:
            change = (c - c.shift(n)).abs()
            path = d1.rolling(n).sum()
            er = change / path
            df[f"er{n}"] = er.replace([np.inf, -np.inf], 0).fillna(0)
        out[sym] = df
    return out


def _pct_rank(v, mask):
    sub = v[mask]
    out = np.full(len(v), np.nan)
    out[mask] = sub.argsort().argsort() / max(len(sub) - 1, 1)
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


def report(mask, ret, label):
    st = _stats(np.where(mask, ret, np.nan))
    if st is None:
        print(f"  {label:<26} n<{MIN_N}")
        return None
    print(f"  {label:<26} n={st['n']:>6,} 胜率={st['win']*100:>6.1f}% 期望={st['mean']*100:>+7.2f}% "
          f"中位={st['median']*100:>+6.2f}% PF={st['pf']:>5.1f}")
    return st


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    vr = np.array([r["vr"] for r in rows], dtype=float)
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    ret_c = _returns_vec(arr, -1, 12, 0.50, 0.10, None)

    syms = sorted({r["symbol"] for r in rows})
    print(f"加载日线算 ER...", flush=True)
    daily = _load_daily(syms)

    er = {n: np.full(len(rows), np.nan) for n in N_ER}
    for i, r in enumerate(rows):
        df = daily.get(r["symbol"])
        if df is None:
            continue
        d = r["eo"].date() - timedelta(days=1)
        if d in df.index:
            for n in N_ER:
                v = float(df.loc[d, f"er{n}"])
                if v == v:
                    er[n][i] = v
    have = ~np.isnan(er[20])
    print(f"ER 可关联事件: {int(have.sum()):,} / {len(rows):,}\n")

    for n in N_ER:
        e = er[n]
        print("=" * 100)
        print(f"■ ER n={n} 天 × A/B/C 信号分桶")
        print("=" * 100)
        for label, m, ret in (
            ("A 信号(≥50% 做多)", chg >= 0.50, ret_ab),
            ("B 信号(20~50% 做多)", (0.20 <= chg) & (chg < 0.50), ret_ab),
            ("C 信号(<5% 做空)", chg < 0.05, ret_c),
        ):
            print(f"\n◆ {label}")
            report(m & have, ret, "可算 ER 子集")
            for bname, f in ER_BINS:
                report(m & have & f(e), ret, bname)
        if n == 20:
            break   # 分桶只看 n=20，避免重复

    # B 三维排序
    n = 20
    e = er[n]
    b = (0.20 <= chg) & (chg < 0.50)
    s2 = (_pct_rank(chg, b) + _pct_rank(vr, b)) / 2
    s3 = (_pct_rank(chg, b) + _pct_rank(vr, b) + _pct_rank(e, b & have)) / 3
    print("\n" + "=" * 100)
    print("■ B 信号排序对比（Q1 最低 ~ Q5 最高）")
    print("=" * 100)
    for label, s in (("二维(涨幅+vr)", s2), ("三维(涨幅+vr+ER)", s3)):
        print(f"\n◆ {label}")
        for i in range(5):
            lo, hi = i / 5, (i + 1) / 5
            m = b & have & (s >= lo) & (s < hi)
            report(m, ret_ab, f"Q{i+1} {'最低' if i==0 else '最高' if i==4 else f'{int(lo*100)}~{int(hi*100)}pct'}")
    # 三维 Q5 与二维 Q5 对比 + 前10%
    m3 = b & have & (s3 >= 0.8)
    m2 = b & have & (s2 >= 0.8)
    print("\n◆ 头部对比")
    report(m2, ret_ab, "二维 Q5(前20%)")
    report(m3, ret_ab, "三维 Q5(前20%)")
    report(b & have & (s3 >= 0.9), ret_ab, "三维 前10%")
    if m3.sum() >= MIN_N:
        print(f"  三维 Q5 区间: chg24[{chg[m3].min()*100:.0f}%,{chg[m3].max()*100:.0f}%] "
              f"vr[{vr[m3].min():.1f}~] ER[{e[m3].min():.2f}~{e[m3].max():.2f}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
