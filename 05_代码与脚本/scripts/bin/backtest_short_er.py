#!/usr/bin/env python3
"""「涨幅榜 + 低 ER（震荡路径）」做空信号回测。

假设（用户 2026-10-07）：
  涨幅榜内的币，若涨得"脏"（ER 低=路径来回震荡、非直线上涨），可能是诱多/出货：
    A. 涨幅↑ + vr↑ + ER↓  放量但震荡 → 放量出货嫌疑
    B. 涨幅↑ + vr↓ + ER↓  缩量但震荡 → 无量虚涨，之后回落
  用做空（FIX 12h/TP50/SL10，与 TRAP_SHORT 参数一致）验证是否正期望。

口径：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - ER：日线 er10/20/30，事件取前一交易日（无前视）
  - 池：涨幅榜（chg24≥20%），分 A(≥50%)/B(20~50%)
  - 做空收益 = _returns_vec(-1, 12, 0.5, 0.1)；做多收益 = TRAIL3；成本 0.3%

用法：python bin/backtest_short_er.py
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


def report(mask, ret, label, flip=False):
    """flip=True 时把做多收益翻转为做空视角展示（对照用）。"""
    r = ret.copy() if flip else ret
    st = _stats(np.where(mask, r, np.nan))
    if st is None:
        print(f"  {label:<34} n<{MIN_N}")
        return
    print(f"  {label:<34} n={st['n']:>6,} 胜率={st['win']*100:>6.1f}% 期望={st['mean']*100:>+7.2f}% "
          f"中位={st['median']*100:>+6.2f}% PF={st['pf']:>5.1f} 最差={st['worst']*100:>+6.1f}%")


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]
    vr = np.array([r["vr"] for r in rows], dtype=float)
    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1          # 做多 TRAIL3
    ret_short = _returns_vec(arr, -1, 12, 0.50, 0.10, None)        # 做空 FIX12h

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

    n = 20
    e = er[n]
    have = ~np.isnan(e)

    print("\n" + "=" * 108)
    print(f"「涨幅榜 + ER 低(震荡)」做空验证（ER n={n}，做空 FIX12h/TP50/SL10，含 0.3% 成本）")
    print("=" * 108)

    for pname, pool in (
        ("涨幅≥50%(A)", chg >= 0.50),
        ("涨幅20~50%(B)", (0.20 <= chg) & (chg < 0.50)),
        ("涨幅≥20%(A+B)", chg >= 0.20),
    ):
        print(f"\n■ 池：{pname}")
        report(pool & have, ret_short, "全池做空基准")
        report(pool & have, ret_ab, "全池做多对照(TRAIL3)")
        for lo, hi, erl in ((0.0, 0.2, "ER<0.2"), (0.2, 0.4, "ER 0.2~0.4"), (0.0, 0.4, "ER<0.4 合并")):
            m = pool & have & (e >= lo) & (e < hi)
            print(f"  ── {erl}（震荡路径）──")
            report(m, ret_short, "  做空(FIX12h)")
            report(m, ret_ab, "  做多对照(TRAIL3)")
            report(m & (vr < 3.0), ret_short, f"  {erl} + vr↓(<3.0) 做空")
            report(m & (vr >= 3.0), ret_short, f"  {erl} + vr↑(≥3.0) 做空")
            report(m & (vr >= 5.0), ret_short, f"  {erl} + vr↑↑(≥5.0) 做空")

    # 直线上涨对照（ER 高）做空应差
    print("\n■ 对照：涨幅榜 + ER 高（直线）做空（预期无效/反向）")
    for pname, pool in (("涨幅≥50%(A)", chg >= 0.50), ("涨幅20~50%(B)", (0.20 <= chg) & (chg < 0.50))):
        m = pool & have & (e >= 0.6)
        report(m, ret_short, f"{pname} ER≥0.6 做空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
