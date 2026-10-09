#!/usr/bin/env python3
"""B/C 信号 × 乖离率（1000 根 1h K 线 ≈ 41.7 日均线）过滤回测。

口径（2026-10-07）：
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - 乖离率 bias = 事件收盘(entry) / 前1000根1h收盘均值 - 1（入场时可得，无前视）
  - B 信号（20~50% 做多）：TRAIL3 出场（ph_tr3×0.97/entry-1）
  - C 信号（<5% 做空）：FIX 12h / TP50% / SL10%（与实盘 TRAP_SHORT 参数一致）
  - 成本 0.3%；验证是否能用乖离过滤把 B/C 调到可实盘

用法：python bin/backtest_bias_filter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
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
BATCH = 2000
FIX_N, FIX_TP, FIX_SL = 12, 0.50, 0.10

SQL_MA = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT ev.symbol, ev.open_time, ma.ma1000
FROM ev
LEFT JOIN LATERAL (
    SELECT AVG(q.close_px) AS ma1000 FROM biz.asset_klines q
    WHERE q.symbol = ev.symbol AND q.interval = '1h' AND q.open_px > 0
      AND q.open_time >= ev.open_time - INTERVAL '1000 hours'
      AND q.open_time < ev.open_time
) ma ON true
"""

BIAS_BINS = [
    ("<-10%", lambda b: b < -0.10),
    ("-10%~0", lambda b: (-0.10 <= b) & (b < 0)),
    ("0~10%", lambda b: (0 <= b) & (b < 0.10)),
    ("10%~25%", lambda b: (0.10 <= b) & (b < 0.25)),
    ("25%~50%", lambda b: (0.25 <= b) & (b < 0.50)),
    ("50%~100%", lambda b: (0.50 <= b) & (b < 1.00)),
    (">=100%", lambda b: b >= 1.00),
]


def _vals(rows) -> str:
    return ",".join("('{}','{}'::timestamptz)".format(s, t.isoformat().replace("'", "''")) for s, t in rows)


def _fetch_ma(events, idx):
    out = {}
    sel = [(events[i]["symbol"], events[i]["eo"]) for i in idx]
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(sel), BATCH):
                chunk = sel[k:k + BATCH]
                cur.execute(SQL_MA.format(v=_vals(chunk)))
                for r in cur.fetchall():
                    out[(r[0], r[1])] = r
    return out


def _stats(ret, mask):
    r = ret[mask]
    r = r[~np.isnan(r)]
    if len(r) < MIN_N:
        return None
    rn = r - COST
    win = rn > 0
    gw, gl = float(rn[win].sum()), abs(float(rn[~win].sum())) if (~win).any() else 0.0
    return {
        "n": len(r), "win": float((rn > 0).mean()), "mean": float(rn.mean()),
        "pf": gw / gl if gl > 0 else float("inf"),
        "worst": float(np.min(rn)),
    }


def run_signal(events, arr, label, mask, ret):
    n_total = int(mask.sum())
    print(f"\n■ {label}  n={n_total:,}")
    idx = np.where(mask)[0]
    ma_map = _fetch_ma(events, idx)
    bias = np.full(len(events), np.nan)
    for i in idx:
        key = (events[i]["symbol"], events[i]["eo"])
        r = ma_map.get(key)
        if r and r[2] is not None:
            ma = float(r[2])
            if ma > 0:
                bias[i] = float(arr["entry"][i]) / ma - 1
    have = ~np.isnan(bias)
    print(f"  乖离可算: {int((have & mask).sum()):,}（{int((have & mask).sum()) / max(n_total,1) * 100:.0f}%）"
          f" | 基准净期望(可算子集): ", end="")
    base = _stats(ret, have & mask)
    print(f"{base['mean'] * 100:+.2f}%" if base else "n/a")

    print(f"  {'乖离区间':>10}{'n':>8}{'胜率%':>8}{'净期望%':>10}{'PF':>8}{'最差%':>8}")
    for bname, f in BIAS_BINS:
        st = _stats(ret, mask & have & f(bias))
        if st is None:
            print(f"  {bname:>10} n 不足(<{MIN_N})")
        else:
            print(f"  {bname:>10}{st['n']:>8,}{st['win']*100:>8.1f}{st['mean']*100:>10.2f}"
                  f"{st['pf']:>8.1f}{st['worst']*100:>8.1f}")
    return bias, have


def main() -> int:
    rows = load_events()          # eo 为 datetime
    arr = _vec_rows(rows)
    events = [{k: v for k, v in r.items()} for r in rows]
    chg = arr["chg24"]

    # B：TRAIL3 出场
    b_ret = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    # C：FIX 12h/TP50/SL10 做空
    c_ret = _returns_vec(arr, -1, FIX_N, FIX_TP, FIX_SL, None)

    b_mask = (0.20 <= chg) & (chg < 0.50)
    c_mask = chg < 0.05

    run_signal(events, arr, "B 信号（20~50% 做多）· TRAIL3", b_mask, b_ret)
    run_signal(events, arr, "C 信号（<5% 做空）· FIX12h/TP50/SL10", c_mask, c_ret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
