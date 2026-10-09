#!/usr/bin/env python3
"""VOL_ratio（按币自身 taker 成交额中位归一化）因子 × A/B/C 信号回测 + 深缩量反弹验证。

VOL_ratio（用户口径，§3.10）：
  taker_total = taker_buy_usd + taker_sell_usd（biz.cg_taker_volume_hist，4h 桶）
  vol_med[sym] = median(该币全部 4h 桶 taker_total)   ← 按币各自归一化
  VOL_ratio   = 桶 taker_total / vol_med[sym]
  含义：<0.30 深度缩量（→ 反弹），>3.00 深度放量

口径（2026-10-07）：
  - 数据：cg_taker_volume_hist 4h 桶，2025-10-11 ~ 2026-10-07（约 1 年，529 合约）
  - 事件：放量大阳（chg1h≥3% & vr≥2），缓存 trade_params_events.csv
  - 关联：事件取**已结束的最近 4h 桶**（桶起点+4h ≤ 事件时点，无前视）的 VOL_ratio
  - 出场：A/B 用 TRAIL3（ph_tr3×0.97/entry-1）；C 用 FIX 12h/TP50/SL10；成本 0.3%
  - 辅助：独立验证「深缩量→96h 反弹」全桶分析（该币所有 4h 桶，非仅事件）

用法：python bin/backtest_vol_ratio.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta
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
MIN_N = 30
BUCKET_H = 4

SQL_TAKER = """
SELECT symbol, ts, taker_buy_usd, taker_sell_usd
FROM biz.cg_taker_volume_hist
WHERE interval = '4h' AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL
  AND symbol = ANY(%s)
ORDER BY symbol, ts
"""

BINS = [
    ("<0.30 深缩量", lambda r: r < 0.30),
    ("0.30~0.70 缩量", lambda r: (0.30 <= r) & (r < 0.70)),
    ("0.70~1.30 平量", lambda r: (0.70 <= r) & (r < 1.30)),
    ("1.30~3.00 放量", lambda r: (1.30 <= r) & (r < 3.00)),
    (">3.00 深放量", lambda r: r >= 3.00),
]


def _stats(ret):
    r = ret[~np.isnan(ret)]
    if len(r) < MIN_N:
        return None
    rn = r - COST
    win = rn > 0
    gw, gl = float(rn[win].sum()), abs(float(rn[~win].sum())) if (~win).any() else 0.0
    return {"n": len(r), "win": float((rn > 0).mean()), "mean": float(rn.mean()),
            "pf": gw / gl if gl > 0 else float("inf"), "worst": float(np.min(rn))}


def report(mask, st_arr, ret, label):
    st = _stats(np.where(mask, ret, np.nan))
    if st is None:
        print(f"  {label:<22} 样本不足(<{MIN_N})")
        return
    print(f"  {label:<22} n={st['n']:>6,} 胜率={st['win']*100:>6.1f}% 期望={st['mean']*100:>+7.2f}% "
          f"PF={st['pf']:>5.1f} 最差={st['worst']*100:>+6.1f}%")


def main() -> int:
    rows = load_events()
    arr = _vec_rows(rows)
    chg = arr["chg24"]

    # 拉 4h taker 桶（分批规避 PG 慢计划）
    syms_all = sorted({r["symbol"] for r in rows})
    buckets = []
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for k in range(0, len(syms_all), 50):
                cur.execute(SQL_TAKER, (syms_all[k:k + 50],))
                buckets.extend(cur.fetchall())
                print(f"  taker 批次 {k//50 + 1}/{(len(syms_all) - 1)//50 + 1} "
                      f"(累计 {len(buckets):,})", flush=True)
    print(f"4h 桶: {len(buckets):,}")

    per = defaultdict(list)
    for sym, ts, buy, sell in buckets:
        per[sym].append((ts, float(buy) + float(sell)))
    med = {s: float(np.median([v for _, v in lst])) for s, lst in per.items()}
    for s in per:
        per[s].sort()

    # 事件 → VOL_ratio
    vr = np.full(len(rows), np.nan)
    for i, r in enumerate(rows):
        lst = per.get(r["symbol"])
        if not lst or r["symbol"] not in med or med[r["symbol"]] <= 0:
            continue
        eo = r["eo"] - timedelta(hours=BUCKET_H)   # 要求桶已结束
        ts_arr = [x[0] for x in lst]
        # 二分找 eo 前最近已结束桶（桶起点 ts 满足 ts + 4h <= r.eo）
        lo, hi = 0, len(lst)
        while lo < hi:      # 找最后一个 ts + 4h <= r.eo
            mid = (lo + hi) // 2
            if ts_arr[mid] + timedelta(hours=BUCKET_H) <= r["eo"]:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            continue
        vr[i] = lst[lo - 1][1] / med[r["symbol"]]

    have = ~np.isnan(vr)
    print(f"VOL_ratio 可关联事件: {int(have.sum()):,} / {len(rows):,}")

    ret_ab = arr["ph_tr3"] * (1 - TR) / arr["entry"] - 1
    ret_c = _returns_vec(arr, -1, 12, 0.50, 0.10, None)

    print("\n" + "=" * 100)
    print("A/B/C 信号 × VOL_ratio 分桶（净收益含 0.3% 成本 · 2025-10 起子样本）")
    print("=" * 100)

    for label, m in (
        ("A 信号(≥50% 做多)", chg >= 0.50),
        ("B 信号(20~50% 做多)", (0.20 <= chg) & (chg < 0.50)),
        ("C 信号(<5% 做空)", chg < 0.05),
    ):
        ret = ret_ab if "做多" in label else ret_c
        print(f"\n■ {label}")
        report(m & have, vr, ret, "可算 VOL_ratio 子集")
        for bname, f in BINS:
            report(m & have & f(vr), vr, ret, bname)

    # 独立验证：深缩量→96h 反弹（全 4h 桶）
    print("\n" + "=" * 100)
    print("独立验证：VOL_ratio<0.30 深缩量 → 后 96h 反弹（该币所有 4h 桶，非仅事件）")
    print("=" * 100)
    # 需要前向价格：用事件缓存不可行（桶不是事件），改 SQL 聚合
    sql_ind = """
WITH t AS (
    SELECT symbol, ts, taker_buy_usd + taker_sell_usd AS tv
    FROM biz.cg_taker_volume_hist
    WHERE interval = '4h' AND taker_buy_usd IS NOT NULL AND taker_sell_usd IS NOT NULL
),
m AS (
    SELECT symbol, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY tv) AS med
    FROM t GROUP BY symbol
),
k AS (
    SELECT symbol, open_time AS ts, close_px,
           LEAD(close_px, 96) OVER (PARTITION BY symbol ORDER BY open_time) AS c96
    FROM biz.asset_klines
    WHERE interval = '1h' AND close_px > 0
),
j AS (
    SELECT t.symbol, t.ts, t.tv / m.med AS ratio, k.c96 / k.close_px - 1 AS r96
    FROM t JOIN m ON m.symbol = t.symbol
    JOIN k ON k.symbol = t.symbol AND k.ts = t.ts
    WHERE m.med > 0
)
SELECT CASE WHEN ratio < 0.30 THEN '深缩量<0.30'
            WHEN ratio < 0.70 THEN '缩量0.30~0.70'
            WHEN ratio < 1.30 THEN '平量0.70~1.30'
            WHEN ratio < 3.00 THEN '放量1.30~3.00'
            ELSE '深放量>3.00' END AS bin,
       COUNT(*), AVG((r96 > 0)::int), AVG(r96), PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r96)
FROM j WHERE r96 IS NOT NULL GROUP BY 1 ORDER BY 1
"""
    with psycopg.connect(get_settings(require_database=True).database_url,
                         connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_ind)
            print(f"  {'桶':<20}{'n':>8}{'96h胜率%':>9}{'96h均值%':>10}{'96h中位%':>10}")
            for r in cur.fetchall():
                print(f"  {r[0]:<20}{int(r[1]):>8,}{float(r[2])*100:>9.1f}"
                      f"{float(r[3])*100:>10.2f}{float(r[4])*100:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
