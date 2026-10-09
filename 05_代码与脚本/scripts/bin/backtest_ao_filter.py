#!/usr/bin/env python3
"""AO（Awesome Oscillator，量纲归一）因子 × A/B/C 信号回测。

AO = ((SMA_s(mid) − SMA_l(mid)) / SMA_l) / ATR(l)，mid=(high+low)/2
  AO>0 = 短期动量强于长期（方向看多）；量纲归一后跨币可比
参数：主 (5,34)，敏感性 (5,20)/(10,40)

口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - AO：日线（每天最后 1h high/low/close）算 ao_sl；事件取**前一交易日**（无前视）
  - 出场：A/B 用 TRAIL3；C 用 FIX12h；成本 0.3%
  - ① AO 正负方向分桶；② AO 五档 rank；③ 过滤规则扫描

用法：python bin/backtest_ao_filter.py
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
AO_PARAMS = [((5, 34), "ao5_34"), ((5, 20), "ao5_20"), ((10, 40), "ao10_40")]

SQL_DAILY = """
SELECT DISTINCT ON (symbol, DATE(open_time)) symbol, DATE(open_time) AS d, high_px, low_px, close_px
FROM biz.asset_klines
WHERE interval = '1h' AND close_px > 0
  AND open_time >= '2022-12-01' AND symbol = ANY(%s)
ORDER BY symbol, DATE(open_time), open_time DESC
"""


def _atr(df, n):
    prev = df["close"].shift(1).fillna(df["close"])
    tr = np.maximum(df["high"] - df["low"],
                    np.maximum((df["high"] - prev).abs(), (df["low"] - prev).abs()))
    return pd.Series(tr).rolling(n, min_periods=1).mean()


def _load_daily(syms) -> dict[str, pd.DataFrame]:
    dailies = []
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms), 50):
                cur.execute(SQL_DAILY, (syms[k:k + 50],))
                dailies.extend(cur.fetchall())
    per = defaultdict(list)
    for sym, d, hi, lo, cl in dailies:
        per[sym].append((d, float(hi), float(lo), float(cl)))
    out = {}
    eps = 1e-12
    for sym, pairs in per.items():
        pairs.sort()
        df = pd.DataFrame(pairs, columns=["d", "high", "low", "close"]).set_index("d")
        mid = (df["high"] + df["low"]) / 2.0
        for (s, l), col in AO_PARAMS:
            sma_s = mid.rolling(s, min_periods=1).mean()
            sma_l = mid.rolling(l, min_periods=1).mean()
            atr_l = _atr(df, l)
            ao = ((sma_s - sma_l) / (sma_l + eps)) / (atr_l + eps)
            df[col] = ao.replace([np.inf, -np.inf], 0).fillna(0)
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
    print("加载日线算 AO...", flush=True)
    daily = _load_daily(syms)

    ao = {col: np.full(len(rows), np.nan) for _, col in AO_PARAMS}
    for i, r in enumerate(rows):
        df = daily.get(r["symbol"])
        if df is None:
            continue
        d = r["eo"].date() - timedelta(days=1)
        if d in df.index:
            for _, col in AO_PARAMS:
                v = float(df.loc[d, col])
                if v == v:
                    ao[col][i] = v
    have = ~np.isnan(ao["ao5_34"])
    print(f"AO 可关联事件: {int(have.sum()):,} / {len(rows):,}\n")

    col = "ao5_34"
    v = ao[col]
    print("=" * 108)
    print("AO(5,34) × A/B/C：正负方向 + 五档 rank")
    print("=" * 108)
    for sig, m, ret in (
        ("A 做多(≥50%)", chg >= 0.50, ret_ab),
        ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50), ret_ab),
        ("C 做空(<5%)", chg < 0.05, ret_c),
    ):
        print(f"\n◆ {sig}")
        base = _stats(np.where(m & have, ret, np.nan))
        print(f"  全池(可算AO) {base['mean']*100:+.2f}% PF={base['pf']:.1f} n={base['n']:,}")
        for name, cond in (
            ("AO<0 空头动量", v < 0),
            ("AO>0 多头动量", v > 0),
            ("AO>0.5 强多头", v > 0.5),
            ("AO>1.0", v > 1.0),
            ("AO<−0.5 强空头", v < -0.5),
        ):
            st = _stats(np.where(m & have & cond, ret, np.nan))
            if st:
                print(f"  {name:<22} n={st['n']:>6,} 胜率={st['win']*100:>5.1f}% "
                      f"期望={st['mean']*100:>+7.2f}% PF={st['pf']:>5.1f}")
        rk = _pct_rank(v, m & have)
        print("  五档 rank:")
        for i in range(5):
            lo, hi = i / 5, (i + 1) / 5
            st = _stats(np.where(m & have & (rk >= lo) & (rk < hi), ret, np.nan))
            if st:
                print(f"    Q{i+1} n={st['n']:>6,} 期望={st['mean']*100:>+7.2f}% PF={st['pf']:>5.1f}")

    print("\n" + "=" * 108)
    print("AO 过滤规则扫描（A/B 相对全池提升，参数敏感性）")
    print("=" * 108)
    for col, pname in [("ao5_34", "(5,34)"), ("ao5_20", "(5,20)"), ("ao10_40", "(10,40)")]:
        v = ao[col]
        have_c = ~np.isnan(v)
        print(f"\n■ AO{pname}")
        for sig, m, ret in (("A 做多(≥50%)", chg >= 0.50, ret_ab),
                            ("B 做多(20~50%)", (0.20 <= chg) & (chg < 0.50), ret_ab)):
            base = _stats(np.where(m & have_c, ret, np.nan))
            print(f"  {sig}: 全池 {base['mean']*100:+.2f}% (PF {base['pf']:.1f}, n={base['n']:,})")
            best = []
            for gname, cond in (
                ("只留 AO>0", v > 0), ("只留 AO>0.5", v > 0.5), ("只留 AO>1.0", v > 1.0),
                ("只留 AO<0", v < 0), ("只留 AO<−0.5", v < -0.5),
            ):
                st = _stats(np.where(m & have_c & cond, ret, np.nan))
                if st and st["n"] >= 100:
                    best.append((st["mean"], gname, st))
            best.sort(reverse=True)
            for mean, gname, st in best[:3]:
                print(f"    {gname:<14} {mean*100:+.2f}% (PF {st['pf']:.1f}, n={st['n']:,}) "
                      f"↑{(mean-base['mean'])*100:+.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
