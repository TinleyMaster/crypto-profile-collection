#!/usr/bin/env python3
"""假设验证：上升趋势（24h 涨幅榜档位）中的代币，**小周期出现「多空平衡」K 线** 是否为好的做多入场点。

口径（2026-10-07）：
  - 周期：1h（biz.asset_klines，2024-10-07 起，529 合约）。5m/15m 仅 2026-09-16 起约 3 周，
    样本不足，故「小周期」取 1h。
  - 趋势：chg24 = close_px / LAG(close_px,24) - 1（事件 bar 收盘 / 24h 前 bar 收盘），
    即**事件时点可得的滚动 24h 涨幅**，对齐线上 `ticker/24hr`。按涨幅榜档位分桶。
  - 入场：该平衡 bar 的收盘价；前向 r24 = LEAD(close_px,24)/close_px-1，r168 同理。
    ⚠️ 所有「平衡」特征只用**当根及历史 bar**，无前视。

「多空平衡」四个定义（均只做多视角）：
  A_doji   十字星/小实体+影线均衡：实体占振幅 ≤25% 且上下影线差 ≤35% 振幅
           —— 价格被多空双向推动但收回原位，最贴近「力量平衡」
  B_quiet  小实体 + 缩量：实体占振幅 ≤25% 且 量比 <1（相对前 20 根均量）
           —— 平衡且无资金推动（蓄势）
  C_mid    收盘落在近 24 根高低区间的中点（40%~60% 分位）—— 拉锯中枢
  D_cvd    主动买≈主动卖（|净主动占比| ≤5%）—— 真·资金口径，但数据仅 6 周，**观察性、不作结论**

基准：同档位不加平衡条件的 `ALL` 行 —— 用于判断「平衡」是否真的带来增量，
     否则无法区分「趋势本身的收益」与「平衡形态的贡献」。

三口径加固（§7）：扣 BTC β（β=1，多头减）、独立事件去重（每币每日首事件）、成本 0.1%/0.3%。

用法：python bin/backtest_balance_bar_long.py
"""
from __future__ import annotations

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
OUT = DATA_DIR / "backtest_balance_bar_long.csv"

COST_1, COST_3 = 0.001, 0.003

# ---------------------------------------------------------------- 主回测 SQL
SQL = """
WITH k AS (
    SELECT symbol, open_time, close_px, quote_vol,
           (high_px - low_px)                        AS rng,
           ABS(close_px - open_px)                   AS body,
           (high_px - GREATEST(open_px, close_px))   AS upsh,
           (LEAST(open_px, close_px) - low_px)       AS dnsh,
           close_px / NULLIF(LAG(close_px, 24)
                             OVER (PARTITION BY symbol ORDER BY open_time), 0) - 1 AS chg24,
           LEAD(close_px, 24)  OVER (PARTITION BY symbol ORDER BY open_time) AS px24,
           LEAD(close_px, 168) OVER (PARTITION BY symbol ORDER BY open_time) AS px168,
           AVG(quote_vol)      OVER (PARTITION BY symbol ORDER BY open_time
                                     ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS vol20,
           MIN(low_px)         OVER (PARTITION BY symbol ORDER BY open_time
                                     ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS lo24,
           MAX(high_px)        OVER (PARTITION BY symbol ORDER BY open_time
                                     ROWS BETWEEN 23 PRECEDING AND CURRENT ROW) AS hi24,
           ROW_NUMBER()        OVER (PARTITION BY symbol, DATE(open_time)
                                     ORDER BY open_time) AS rn
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0 AND quote_vol IS NOT NULL
),
btc AS (
    SELECT open_time,
           LEAD(close_px, 24)  OVER (ORDER BY open_time) / close_px - 1 AS btc_r24,
           LEAD(close_px, 168) OVER (ORDER BY open_time) / close_px - 1 AS btc_r168
    FROM biz.asset_klines
    WHERE interval = '1h' AND symbol = 'BTCUSDT' AND close_px > 0
),
f AS (
    SELECT s.rng > 0 AND s.body / s.rng <= 0.25
                 AND ABS(s.upsh - s.dnsh) / s.rng <= 0.35          AS m_doji,
           s.rng > 0 AND s.body / s.rng <= 0.25
                 AND s.vol20 > 0 AND s.quote_vol / s.vol20 < 1.0   AS m_quiet,
           s.hi24 > s.lo24
                 AND (s.close_px - s.lo24) / NULLIF(s.hi24 - s.lo24, 0)
                     BETWEEN 0.40 AND 0.60                          AS m_mid,
           s.rn,
           s.px24  / s.close_px - 1 AS r24,
           s.px168 / s.close_px - 1 AS r168,
           b.btc_r24, b.btc_r168,
           CASE WHEN s.chg24 < 0.05 THEN '0_g0-5'
                WHEN s.chg24 < 0.20 THEN '1_g5-20'
                WHEN s.chg24 < 0.50 THEN '2_g20-50'
                WHEN s.chg24 < 0.75 THEN '3_g50-75'
                ELSE                    '4_g75+' END AS tb
    FROM k s
    LEFT JOIN btc b ON b.open_time = s.open_time
    WHERE s.chg24 IS NOT NULL
),
u AS (
    SELECT 'ALL'::text     AS mark, tb, r24, r168, btc_r24, btc_r168, rn FROM f
    UNION ALL SELECT 'A_doji',  tb, r24, r168, btc_r24, btc_r168, rn FROM f WHERE m_doji
    UNION ALL SELECT 'B_quiet', tb, r24, r168, btc_r24, btc_r168, rn FROM f WHERE m_quiet
    UNION ALL SELECT 'C_mid',   tb, r24, r168, btc_r24, btc_r168, rn FROM f WHERE m_mid
)
SELECT mark, tb,
       COUNT(r24)                                        AS n24,
       AVG((r24  > 0)::int)                              AS w24,
       AVG(r24)                                          AS m24,
       AVG(r24)  FILTER (WHERE rn = 1)                   AS m24_ind,
       COUNT(r168)                                       AS n168,
       AVG((r168 > 0)::int)                              AS w168,
       AVG(r168)                                         AS m168,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r168) AS md168,
       AVG(r168 - btc_r168)                              AS m168_net,
       COUNT(*) FILTER (WHERE rn = 1)                    AS n_ind,
       AVG(r168) FILTER (WHERE rn = 1)                   AS m168_ind
FROM u
GROUP BY mark, tb
ORDER BY tb, mark
"""

# ------------------------------------------------- CVD 观察性回测（仅 6 周）
SQL_CVD = """
WITH s AS (
    SELECT symbol, date_trunc('hour', ts) AS h,
           SUM(cvd_5m_usd) AS cvd, SUM(vol_5m_usd) AS vol
    FROM biz.oi_cvd_snapshot
    GROUP BY 1, 2
),
c AS (
    SELECT symbol, h, cvd / NULLIF(vol, 0) AS net_cvd
    FROM s WHERE vol > 0
),
k AS (
    SELECT symbol, open_time, close_px,
           close_px / NULLIF(LAG(close_px, 24)
                             OVER (PARTITION BY symbol ORDER BY open_time), 0) - 1 AS chg24,
           LEAD(close_px, 24)  OVER (PARTITION BY symbol ORDER BY open_time) AS px24,
           LEAD(close_px, 168) OVER (PARTITION BY symbol ORDER BY open_time) AS px168
    FROM biz.asset_klines
    WHERE interval = '1h' AND close_px > 0
),
j AS (
    SELECT CASE WHEN ABS(c.net_cvd) <= 0.05 THEN 'D_cvd_balanced' ELSE 'D_cvd_tilted' END AS mark,
           k.px24 / k.close_px - 1  AS r24,
           k.px168 / k.close_px - 1 AS r168
    FROM k JOIN c ON c.symbol = k.symbol AND c.h = k.open_time
    WHERE k.chg24 >= 0.20
)
SELECT mark, COUNT(r24) AS n24, AVG((r24 > 0)::int) AS w24, AVG(r24) AS m24,
       COUNT(r168) AS n168, AVG((r168 > 0)::int) AS w168, AVG(r168) AS m168
FROM j GROUP BY mark ORDER BY mark
"""

COLS = ["mark", "tb", "n24", "w24", "m24", "m24_ind", "n168", "w168", "m168", "md168",
        "m168_net", "n_ind", "m168_ind"]

TB_ORDER = ["0_g0-5", "1_g5-20", "2_g20-50", "3_g50-75", "4_g75+"]
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
    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL)
            rows = cur.fetchall()
            cur.execute(SQL_CVD)
            cvd_rows = cur.fetchall()

    recs = {}
    for r in rows:
        d = dict(zip(COLS, r))
        for k in COLS:
            if k in ("mark", "tb"):
                continue
            d[k] = None if d[k] is None else float(d[k])
        recs[(d["tb"], d["mark"])] = d

    print("\n=========== 上升趋势 × 多空平衡K线 · 1h · 做多前向收益 ===========")
    print("（趋势档 = 事件时点滚动 24h 涨幅；入场 = 平衡 bar 收盘）\n")

    for tb in TB_ORDER:
        base = recs.get((tb, "ALL"))
        if not base:
            continue
        print(f"■ 涨幅档 {tb[2:]}  基准 n={int(base['n168']):,}  "
              f"r168 {pct(base['m168'])}% / 胜 {pct(base['w168'],1)}%")
        hdr = (f"  {'平衡定义':<26}{'n':>10}{'胜24%':>8}{'期望24%':>9}"
               f"{'胜168%':>8}{'期望168%':>9}{'中位168%':>9}"
               f"{'| 扣β期望':>10}{'| 独立n':>8}{'独立期望':>9}{'| 成0.3胜':>9}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for mk in MARK_ORDER:
            d = recs.get((tb, mk))
            if not d:
                continue
            print(f"  {MARK_DESC[mk]:<26}{int(d['n168']):>10,}"
                  f"{pct(d['w24'],1):>8}{pct(d['m24']):>9}"
                  f"{pct(d['w168'],1):>8}{pct(d['m168']):>9}{pct(d['md168']):>9}"
                  f"{pct(d['m168_net']):>10}{int(d['n_ind']):>8,}"
                  f"{pct(d['m168_ind']):>9}"
                  f"{pct(None if d['m168'] is None else d['m168'] - COST_3, 1):>9}")
        print()

    print("=== D 主动买卖平衡（|净主动占比| ≤5%）· 涨幅榜 ≥20% · ⚠️仅 6 周观察性样本 ===")
    for r in cvd_rows:
        mark, n24, w24, m24, n168, w168, m168 = r
        print(f"  {mark:<18} n24={int(n24):>6,} 胜24={pct(w24,1):>5}% 期望24={pct(m24):>7}%"
              f"  | n168={int(n168):>6,} 胜168={pct(w168,1):>5}% 期望168={pct(m168):>7}%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=COLS)
        w.writeheader()
        for tb in TB_ORDER:
            for mk in MARK_ORDER:
                if (tb, mk) in recs:
                    w.writerow(recs[(tb, mk)])
    print(f"\n结果已存 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())