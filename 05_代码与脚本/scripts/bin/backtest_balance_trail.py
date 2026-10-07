#!/usr/bin/env python3
"""假设验证（交易口径版）：上升趋势（24h 涨幅榜）中「多空平衡」K 线 + **TRAIL TR=3% 出场**。

背景：`backtest_balance_bar_long.py` 用裸持前向收益否证了该假设，但项目既有结论是
「出场规则的价值高于入场信号」（§2.1 的 +5.80% 即 TRAIL 产物）。故本脚本用与 §2.1 同口径的
TRAIL TR=3% 重测：把「入场时机好不好」落到**实际交易收益**上。

出场规则（与 §2.1 A 信号同口径）：
  - 入场：平衡 bar 收盘价 entry
  - 窗口：T+24h（24 根 1h）
  - 跟踪止盈：stop_i = 0.97 × peak_prev，其中 peak_prev = max(entry, 前 i-1 根的 high)
    ⚠️ 用「前一根为止的峰值」定止损，**不含当根 high**，避免盘中前视
  - 首根 low ≤ stop 即离场（按 stop 价成交）；未触发则第 24 根收盘离场

「多空平衡」定义与趋势档位同 `backtest_balance_bar_long.py`，并保留同档位 `ALL` 基准行。

用法：
    python bin/backtest_balance_trail.py                # chg24 >= 20%（涨幅榜）
    python bin/backtest_balance_trail.py --min-chg 0.05  # 含温和上涨档（样本更大、更慢）
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import psycopg

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402

DATA_DIR = SCRIPT_DIR.parent / "data"
COST = 0.003          # 往返成本（悲观档）
TR = 0.97             # TRAIL TR=3%

SQL = """
WITH k AS (
    SELECT symbol, open_time, open_px, close_px, quote_vol,
           (high_px - low_px)                      AS rng,
           ABS(close_px - open_px)                 AS body,
           (high_px - GREATEST(open_px, close_px)) AS upsh,
           (LEAST(open_px, close_px) - low_px)     AS dnsh,
           close_px / NULLIF(LAG(close_px, 24)
                             OVER (PARTITION BY symbol ORDER BY open_time), 0) - 1 AS chg24,
           LEAD(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) AS px24,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
                                ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS vol20,
           MIN(low_px)  OVER (PARTITION BY symbol ORDER BY open_time
                              ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS lo24,
           MAX(high_px) OVER (PARTITION BY symbol ORDER BY open_time
                              ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS hi24,
           ROW_NUMBER() OVER (PARTITION BY symbol, DATE(open_time)
                              ORDER BY open_time) AS rn
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0 AND quote_vol IS NOT NULL
),
ev AS (
    SELECT k.*,
           k.px24 / k.close_px - 1 AS hold_ret,
           CASE WHEN k.chg24 < 0.20 THEN '1_g5-20'
                WHEN k.chg24 < 0.50 THEN '2_g20-50'
                WHEN k.chg24 < 0.75 THEN '3_g50-75'
                ELSE                    '4_g75+' END AS tb,
           (k.rng > 0 AND k.body / k.rng <= 0.25
                       AND ABS(k.upsh - k.dnsh) / k.rng <= 0.35) AS m_doji,
           (k.rng > 0 AND k.body / k.rng <= 0.25
                       AND k.vol20 > 0 AND k.quote_vol / k.vol20 < 1.0) AS m_quiet,
           (k.hi24 > k.lo24
            AND (k.close_px - k.lo24) / NULLIF(k.hi24 - k.lo24, 0)
                BETWEEN 0.40 AND 0.60) AS m_mid
    FROM k
    WHERE k.chg24 >= %s AND k.px24 IS NOT NULL
),
path AS (
    -- 事件后 24 根 1h 的 high/low 路径（走 (symbol, interval, open_time) 索引）
    SELECT e.symbol, e.open_time, e.close_px, e.hold_ret, e.rn, e.tb,
           e.m_doji, e.m_quiet, e.m_mid,
           f.i, f.hv, f.lv,
           GREATEST(e.close_px,
                    MAX(f.hv) OVER (PARTITION BY e.symbol, e.open_time
                                    ORDER BY f.i
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
           ) AS peak_prev
    FROM ev e
    CROSS JOIN LATERAL (
        SELECT row_number() OVER (ORDER BY k2.open_time) AS i,
               k2.high_px AS hv, k2.low_px AS lv
        FROM biz.asset_klines k2
        WHERE k2.symbol = e.symbol AND k2.interval = '1h'
          AND k2.open_time > e.open_time
        ORDER BY k2.open_time
        LIMIT 24
    ) f
),
res AS (
    SELECT symbol, open_time,
           MIN(close_px)  AS entry,
           MIN(hold_ret)  AS hold_ret,
           MIN(rn)        AS rn,
           MIN(tb)        AS tb,
           BOOL_OR(m_doji)  AS m_doji,
           BOOL_OR(m_quiet) AS m_quiet,
           BOOL_OR(m_mid)   AS m_mid,
           COALESCE((ARRAY_AGG(%s * peak_prev / close_px - 1 ORDER BY i)
                     FILTER (WHERE lv <= %s * peak_prev))[1],
                    MIN(hold_ret)) AS trail_ret,
           COALESCE((ARRAY_AGG(i ORDER BY i)
                     FILTER (WHERE lv <= %s * peak_prev))[1], 24) AS hold_bars,
           COUNT(*) AS bars
    FROM path
    GROUP BY symbol, open_time
),
u AS (
    SELECT 'ALL'::text AS mark, tb, trail_ret, hold_ret, rn, hold_bars FROM res WHERE bars = 24
    UNION ALL SELECT 'A_doji',  tb, trail_ret, hold_ret, rn, hold_bars FROM res WHERE bars = 24 AND m_doji
    UNION ALL SELECT 'B_quiet', tb, trail_ret, hold_ret, rn, hold_bars FROM res WHERE bars = 24 AND m_quiet
    UNION ALL SELECT 'C_mid',   tb, trail_ret, hold_ret, rn, hold_bars FROM res WHERE bars = 24 AND m_mid
)
SELECT mark, tb,
       COUNT(*)                                   AS n,
       AVG((trail_ret > 0)::int)                  AS w,
       AVG(trail_ret)                             AS m,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY trail_ret) AS md,
       SUM(GREATEST(trail_ret - %s, 0))
           / NULLIF(ABS(SUM(LEAST(trail_ret - %s, 0))), 0)    AS pf,
       AVG(hold_bars)                             AS avg_hold,
       AVG(hold_ret)                              AS m_hold,
       COUNT(*) FILTER (WHERE rn = 1)             AS n_ind,
       AVG(trail_ret) FILTER (WHERE rn = 1)       AS m_ind,
       AVG((trail_ret > %s)::int)                 AS w_c
FROM u
GROUP BY mark, tb
ORDER BY tb, mark
"""

COLS = ["mark", "tb", "n", "w", "m", "md", "pf", "avg_hold", "m_hold",
        "n_ind", "m_ind", "w_c"]
TB_ORDER = ["1_g5-20", "2_g20-50", "3_g50-75", "4_g75+"]
MARK_ORDER = ["ALL", "A_doji", "B_quiet", "C_mid"]
MARK_DESC = {
    "ALL": "基准(不加平衡条件)",
    "A_doji": "A 十字星/小实体+影线均衡",
    "B_quiet": "B 小实体+缩量",
    "C_mid": "C 收盘在近24根区间中点",
}


def pct(x, nd=2):
    return "—" if x is None else f"{float(x) * 100:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-chg", type=float, default=0.20, help="chg24 下限（默认 0.20）")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    params = (args.min_chg, TR, TR, TR, COST, COST, COST)
    with psycopg.connect(settings.database_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, params)
            rows = cur.fetchall()

    recs = {}
    for r in rows:
        d = dict(zip(COLS, r))
        for k in COLS:
            if k in ("mark", "tb"):
                continue
            d[k] = None if d[k] is None else float(d[k])
        recs[(d["tb"], d["mark"])] = d

    print(f"\n===== 上升趋势 × 多空平衡K线 + TRAIL TR=3% 出场（窗口 T+24h，chg24 >= {args.min_chg}）=====\n")
    for tb in TB_ORDER:
        b = recs.get((tb, "ALL"))
        if not b:
            continue
        print(f"■ 涨幅档 {tb[2:]}   基准 n={int(b['n']):,}  "
              f"TRAIL {pct(b['m'])}% / 胜 {pct(b['w'],1)}%  ←→ 裸持 {pct(b['m_hold'])}%")
        hdr = (f"  {'平衡定义':<26}{'n':>9}{'胜%':>7}{'期望%':>8}{'中位%':>8}{'PF':>7}"
               f"{'持bar':>7}{'| 裸持期望':>10}{'| 独立n':>8}{'独立期望':>9}{'| 成0.3胜':>9}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for mk in MARK_ORDER:
            d = recs.get((tb, mk))
            if not d:
                continue
            print(f"  {MARK_DESC[mk]:<26}{int(d['n']):>9,}{pct(d['w'],1):>7}"
                  f"{pct(d['m']):>8}{pct(d['md']):>8}{d['pf']:>7.2f}"
                  f"{d['avg_hold']:>7.1f}{pct(d['m_hold']):>10}"
                  f"{int(d['n_ind']):>8,}{pct(d['m_ind']):>9}{pct(d['w_c'],1):>9}")
        print()

    OUT = DATA_DIR / f"backtest_balance_trail_{int(args.min_chg * 100)}.csv"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=COLS)
        w.writeheader()
        for tb in TB_ORDER:
            for mk in MARK_ORDER:
                if (tb, mk) in recs:
                    w.writerow(recs[(tb, mk)])
    print(f"结果已存 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())