#!/usr/bin/env python3
"""S1~S8 多空博弈状态回测：P(价) × OI(持仓) × CVD(主动买卖) × VOL(量)。

口径（2026-10-07）：
  - 事件：放量大阳（chg1h≥3% & vr≥2），A=≥50% / B=20~50%（缓存 trade_params_events.csv）
  - 做多事件 P↑、VOL↑ 恒成立 → 落表状态仅 OI×CVD 四类：
      S1 OI↑CVD↑  真实多头进攻      S2 OI↑CVD↓  诱多（合约杠杆推）
      S5 OI↓CVD↑  获利了结/近尾声    S6 OI↓CVD↓  空头回补/修复
  - 出场：TRAIL 3% 跟踪止盈实际出场（ph_tr3×0.97/entry-1），与实盘一致
  - 数据：
      OI×CVD 四态：biz.oi_cvd_snapshot 2026-08-26 起（仅 6 周 → 观察性子样本）
      OI 单维：   biz.cg_oi_hist 4h，2025-10-11 起（大样本补充）

用法：python bin/backtest_state_s8.py
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402

CACHE = SCRIPT_DIR.parent / "data" / "trade_params_events.csv"
TR = 0.03
COST = 0.003
MIN_N = 30
BATCH = 2000

# 事件 bar 内最新快照的 OI/CVD + 24h 前 OI
SQL_OC = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT ev.symbol, ev.open_time, cur.oi_usd, cur.cvd_1h_usd, prev.oi_usd AS oi_prev
FROM ev
LEFT JOIN LATERAL (
    SELECT s.oi_usd, s.cvd_1h_usd FROM biz.oi_cvd_snapshot s
    WHERE s.symbol = ev.symbol AND s.ts >= ev.open_time AND s.ts < ev.open_time + INTERVAL '1 hour'
    ORDER BY s.ts DESC LIMIT 1
) cur ON true
LEFT JOIN LATERAL (
    SELECT s.oi_usd FROM biz.oi_cvd_snapshot s
    WHERE s.symbol = ev.symbol AND s.ts <= ev.open_time - INTERVAL '24 hours'
    ORDER BY s.ts DESC LIMIT 1
) prev ON true
"""

# OI 单维（大样本）：事件时点最近 4h OI vs 24h 前
SQL_OI = """
WITH ev(symbol, open_time) AS (VALUES {v})
SELECT ev.symbol, ev.open_time, cur.oi_close AS oi_cur, prev.oi_close AS oi_prev
FROM ev
LEFT JOIN LATERAL (
    SELECT o.oi_close FROM biz.cg_oi_hist o
    WHERE o.symbol = ev.symbol AND o.interval = '4h' AND o.ts <= ev.open_time
    ORDER BY o.ts DESC LIMIT 1
) cur ON true
LEFT JOIN LATERAL (
    SELECT o.oi_close FROM biz.cg_oi_hist o
    WHERE o.symbol = ev.symbol AND o.interval = '4h' AND o.ts <= ev.open_time - INTERVAL '24 hours'
    ORDER BY o.ts DESC LIMIT 1
) prev ON true
"""


def _vals(rows) -> str:
    return ",".join("('{}','{}'::timestamptz)".format(s, t.replace("'", "''")) for s, t in rows)


def _fetch(cur, sql, events):
    out = {}
    for i in range(0, len(events), BATCH):
        chunk = events[i:i + BATCH]
        cur.execute(sql.format(v=_vals([(e["symbol"], e["eo"]) for e in chunk])))
        for r in cur.fetchall():
            out[(r[0], r[1])] = r
    return out


def _stats(ret: np.ndarray) -> dict | None:
    rs = ret[~np.isnan(ret)]
    if len(rs) < MIN_N:
        return None
    win = rs > 0
    n_w, n_l = int(win.sum()), int((~win).sum())
    gw = float(rs[win].sum())
    gl = abs(float(rs[~win].sum())) if n_l else 0.0
    return {
        "n": len(rs), "win_rate": float((rs > 0).mean()),
        "mean": float(rs.mean()), "median": float(np.median(rs)),
        "pf": gw / gl if gl > 0 else float("inf"), "worst": float(np.min(rs)),
    }


def report(label, mask, ret, all_n):
    st = _stats(np.where(mask, ret, np.nan))
    if st is None:
        print(f"  {label:<28} n 不足(<{MIN_N})")
        return None
    rn = ret[mask] - COST
    gw = float(rn[rn > 0].sum())
    gl = abs(float(rn[rn <= 0].sum())) if (rn <= 0).any() else 0.0
    print(f"  {label:<28} n={st['n']:>5,} 胜率={st['win_rate']*100:>6.1f}% "
          f"期望={st['mean']*100:>+7.2f}% 中位={st['median']*100:>+6.2f}% PF={st['pf']:>5.1f} "
          f"最差={st['worst']*100:>+6.1f}% | 扣0.3%: 胜率={(rn>0).mean()*100:>5.1f}% 期望={rn.mean()*100:>+6.2f}%")
    return st


def run_signal(events, sig_label, sig_ok):
    sel = [(i, e) for i, e in enumerate(events) if sig_ok(e)]
    if not sel:
        return
    idx = np.array([i for i, _ in sel])
    evs = [e for _, e in sel]
    n = len(sel)
    ret = np.full(n, np.nan)
    for j, (_, e) in enumerate(sel):
        ph = e["ph_tr3"]
        if ph:
            ret[j] = float(ph) * (1 - TR) / float(e["entry"]) - 1

    print(f"\n■ {sig_label}  n={n:,}")
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            print("  关联 OI×CVD 快照（6 周）...")
            oc = _fetch(cur, SQL_OC, evs)
            print("  关联 OI 历史（2025-10 起）...")
            oi = _fetch(cur, SQL_OI, evs)

    oi_up = np.full(n, np.nan)
    cvd_up = np.full(n, np.nan)
    oi_up1 = np.full(n, np.nan)
    for j, e in enumerate(evs):
        key = (e["symbol"], datetime.fromisoformat(e["eo"]))
        r = oc.get(key)
        if r and r[2] is not None and r[4] is not None:
            oi_up[j] = float(r[2]) > float(r[4])
        if r and r[3] is not None:
            cvd_up[j] = float(r[3]) > 0
        r2 = oi.get(key)
        if r2 and r2[2] is not None and r2[3] is not None:
            oi_up1[j] = float(r2[2]) > float(r2[3])

    # 四态（OI×CVD）—— 转 bool（nan>0=False，由 has 掩码排除）
    oi_up_b = oi_up > 0
    cvd_up_b = cvd_up > 0
    oi_up1_b = oi_up1 > 0

    print("\n  ── OI×CVD 四态（2026-08-26 起 6 周 · 观察性子样本）──")
    has = ~np.isnan(oi_up) & ~np.isnan(cvd_up)
    print(f"  状态可判定: n={int(has.sum()):,}（全 {n:,}）")
    report("S1 真实多头进攻 OI↑CVD↑", has & oi_up_b & cvd_up_b, ret, n)
    report("S2 诱多 OI↑CVD↓", has & oi_up_b & ~cvd_up_b, ret, n)
    report("S5 获利了结 OI↓CVD↑", has & ~oi_up_b & cvd_up_b, ret, n)
    report("S6 空头回补 OI↓CVD↓", has & ~oi_up_b & ~cvd_up_b, ret, n)
    report("S1+S6（含 CVD 买盘）", has & cvd_up_b, ret, n)
    report("S2+S5（含 CVD 卖盘）", has & ~cvd_up_b, ret, n)

    print("\n  ── OI 单维（2025-10 起大样本）──")
    has1 = ~np.isnan(oi_up1)
    print(f"  OI 可判定: n={int(has1.sum()):,}")
    report("OI 增仓 >0", has1 & oi_up1_b, ret, n)
    report("OI 减仓 ≤0", has1 & ~oi_up1_b, ret, n)


def main() -> int:
    events = []
    with CACHE.open("r", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            events.append(r)
    print(f"事件缓存: {len(events):,}  {CACHE.name}")

    run_signal(events, "A 信号（≥50% 做多）", lambda e: float(e["chg24"]) >= 0.50)
    run_signal(events, "B 信号（20~50% 做多）", lambda e: 0.20 <= float(e["chg24"]) < 0.50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
