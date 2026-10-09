#!/usr/bin/env python3
"""ER 因子作为过滤信号：A/B/C × ER 阈值规则扫描。

假设（2026-10-07）：
  ① 只做"趋势干净"（ER 高=直线上涨）→ 排除震荡路径的假信号
  ② 只做"刚启动/蓄势"（ER 低=尚未走完）→ 排除已直线拉完的追高
  ③ 剔除中段（震荡不上不下）→ 只留两端

口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - ER：日线 er10/20/30，事件取前一交易日（无前视）
  - 出场：A/B 用 TRAIL3；C 用 FIX12h；成本 0.3%
  - 目标：找出对 A/B 期望/PF 有实质提升的 ER 过滤规则

用法：python bin/backtest_er_gate.py
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

GATES = [
    ("全池（不过滤）", lambda e, n: np.ones_like(e, dtype=bool)),
    ("只留 ER≥0.6 直线", lambda e, n: e >= 0.6),
    ("只留 ER≥0.7", lambda e, n: e >= 0.7),
    ("只留 ER≥0.8 强直线", lambda e, n: e >= 0.8),
    ("只留 ER<0.3 蓄势", lambda e, n: e < 0.3),
    ("只留 ER<0.2 震荡", lambda e, n: e < 0.2),
    ("剔除中段(留<0.3或≥0.6)", lambda e, n: (e < 0.3) | (e >= 0.6)),
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
        c = df["close"]
        d1 = c.diff().abs()
        for n in N_ER:
            change = (c - c.shift(n)).abs()
            path = d1.rolling(n).sum()
            er = change / path
            df[f"er{n}"] = er.replace([np.inf, -np.inf], 0).fillna(0)
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
            "median": float(np.median(rn)), "pf": gw / gl if gl > 0 else float("inf"),
            "worst": float(np.min(rn))}


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    ret_c = _returns_vec(arr, -1, 12, 0.50, 0.10, None)

    syms = sorted({r["symbol"] for r in rows})
    print("加载日线算 ER...", flush=True)
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

    print("\n" + "=" * 110)
    print("ER 过滤规则扫描（含 0.3% 成本；期望% / PF / n）")
    print("=" * 110)
    for n in N_ER:
        e = er[n]
        have = ~np.isnan(e)
        print(f"\n■ ER n={n} 天")
        for sig, mask, ret in (
            ("A 做多(≥50%)", chg >= 0.50, ret_ab),
            ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50), ret_ab),
            ("C 做空(<5%)", chg < 0.05, ret_c),
        ):
            print(f"  ◆ {sig}")
            for gname, f in GATES:
                m = mask & have & f(e, n)
                st = _stats(np.where(m, ret, np.nan))
                if st is None:
                    print(f"    {gname:<28} n<{MIN_N}")
                else:
                    d = (st["mean"] - st["mean"]) * 0  # noqa
                    tag = ""
                    print(f"    {gname:<28} n={st['n']:>6,} 胜率={st['win']*100:>5.1f}% "
                          f"期望={st['mean']*100:>+7.2f}% 中位={st['median']*100:>+6.2f}% "
                          f"PF={st['pf']:>5.1f} 最差={st['worst']*100:>+6.1f}%")

    # 最优过滤汇总（A/B 各自相对全池提升）
    print("\n" + "=" * 110)
    print("最优 ER 过滤 vs 全池（期望提升视角）")
    print("=" * 110)
    for n in N_ER:
        e = er[n]
        have = ~np.isnan(e)
        print(f"\n■ ER n={n}")
        for sig, mask, ret in (("A 做多(≥50%)", chg >= 0.50, ret_ab),
                               ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50), ret_ab)):
            base = _stats(np.where(mask & have, ret, np.nan))
            print(f"  {sig}: 全池 {base['mean']*100:+.2f}% (PF {base['pf']:.1f}, n={base['n']:,})")
            rows_out = []
            for gname, f in GATES[1:]:
                m = mask & have & f(e, n)
                st = _stats(np.where(m, ret, np.nan))
                if st and st["n"] >= 100:
                    rows_out.append((st["mean"], gname, st))
            rows_out.sort(reverse=True)
            for mean, gname, st in rows_out[:3]:
                lift = (mean - base["mean"]) * 100
                print(f"    {gname:<24} {mean*100:+.2f}% (PF {st['pf']:.1f}, n={st['n']:,})  ↑{lift:+.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
