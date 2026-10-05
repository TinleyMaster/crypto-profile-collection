#!/usr/bin/env python3
"""加固验证 · 服务端聚合版（备用，主方案分块下载失败时的兜底）。

网络到远程 DB 不稳定（连接槽位被占满）时，逐行事件下载会卡死。本脚本改为
**服务端全量聚合，只回传分桶汇总行**（每条查询只返回几十行），传输不受网络抖动影响。

与主方案（backtest_robust_checks.py）的差异（口径近似，结果可交叉印证）：
- 独立事件：每币**每日**首事件（服务端 ROW_NUMBER），主方案是每币每 72h
- 其余口径一致：原始 / 扣 BTC β(β=1) / 成本 0.1%、0.3%

产物：scripts/data/backtest_robust_checks_agg.csv + 控制台表
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
OUT = DATA_DIR / "backtest_robust_checks_agg.csv"

BASE = """
WITH volbase AS (
    SELECT symbol, open_time, close_px,
           close_px / open_px - 1 AS chg1h,
           quote_vol,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20,
           LEAD(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) AS px24h,
           close_px / LAG(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) - 1 AS chg24
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0
),
evt AS (
    SELECT symbol, open_time, close_px AS px0, chg1h,
           quote_vol / avg20 AS vr,
           px24h / close_px - 1 AS r24h,
           chg24
    FROM volbase
    WHERE avg20 > 0 AND quote_vol / avg20 >= 1.5 AND abs(chg1h) >= 0.015
      AND px24h IS NOT NULL AND chg24 IS NOT NULL
),
btc AS (
    SELECT open_time, close_px,
           LEAD(close_px, 24) OVER (ORDER BY open_time) AS px24
    FROM biz.asset_klines
    WHERE interval = '1h' AND symbol = 'BTCUSDT' AND open_px > 0
),
ev AS (
    SELECT e.symbol, e.open_time, e.chg1h, e.vr, e.r24h, e.chg24,
           b.px24 / b.close_px - 1 AS btc_r24
    FROM evt e LEFT JOIN btc b ON b.open_time = e.open_time
),
x AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY symbol, DATE(open_time)
                              ORDER BY open_time) AS rn   -- 每币每日首事件（近似独立）
    FROM ev
)
"""

# C 表：涨异动 × 当日涨幅分档 → 做多
C_SQL = BASE + """
SELECT {bucket_expr} AS bucket,
       COUNT(*) AS n,
       AVG(r24h) AS m, percentile_cont(0.5) WITHIN GROUP (ORDER BY r24h) AS med,
       AVG((r24h > 0)::int) AS win,
       AVG(r24h - btc_r24) AS m_net,
       AVG((r24h > btc_r24)::int) AS win_net,
       COUNT(*) FILTER (WHERE rn = 1) AS n_indep,
       AVG(r24h) FILTER (WHERE rn = 1) AS m_indep,
       AVG((r24h > 0)::int) FILTER (WHERE rn = 1) AS win_indep,
       AVG((r24h > 0.001)::int) AS win_c1, AVG((r24h > 0.003)::int) AS win_c3,
       AVG(r24h - 0.001) AS m_c1, AVG(r24h - 0.003) AS m_c3
FROM x
WHERE chg1h >= 0.03 AND vr >= 2
GROUP BY bucket
ORDER BY bucket
"""

# E 表：涨幅≥30% × 有无涨异动 → 做多
E_SQL = BASE + """
SELECT CASE WHEN chg1h >= 0.03 AND vr >= 2 THEN '有涨异动' ELSE '无异动' END AS bucket,
       COUNT(*) AS n,
       AVG(r24h) AS m, percentile_cont(0.5) WITHIN GROUP (ORDER BY r24h) AS med,
       AVG((r24h > 0)::int) AS win,
       AVG(r24h - btc_r24) AS m_net,
       AVG((r24h > btc_r24)::int) AS win_net,
       COUNT(*) FILTER (WHERE rn = 1) AS n_indep,
       AVG(r24h) FILTER (WHERE rn = 1) AS m_indep,
       AVG((r24h > 0)::int) FILTER (WHERE rn = 1) AS win_indep,
       AVG((r24h > 0.001)::int) AS win_c1, AVG((r24h > 0.003)::int) AS win_c3,
       AVG(r24h - 0.001) AS m_c1, AVG(r24h - 0.003) AS m_c3
FROM x
WHERE chg24 >= 0.30
GROUP BY bucket
ORDER BY bucket
"""

# 诱多做空：未上榜(<5%) + 放量大阳 → 做空（对 -r24h 聚合）
TRAP_SQL = BASE + """
SELECT '未上榜放量大阳' AS bucket,
       COUNT(*) AS n,
       AVG(-r24h) AS m, percentile_cont(0.5) WITHIN GROUP (ORDER BY -r24h) AS med,
       AVG((-r24h > 0)::int) AS win,
       AVG(-r24h + btc_r24) AS m_net,
       AVG((-r24h > -btc_r24)::int) AS win_net,
       COUNT(*) FILTER (WHERE rn = 1) AS n_indep,
       AVG(-r24h) FILTER (WHERE rn = 1) AS m_indep,
       AVG((-r24h > 0)::int) FILTER (WHERE rn = 1) AS win_indep,
       AVG((-r24h > 0.001)::int) AS win_c1, AVG((-r24h > 0.003)::int) AS win_c3,
       AVG(-r24h - 0.001) AS m_c1, AVG(-r24h - 0.003) AS m_c3
FROM x
WHERE chg1h >= 0.03 AND vr >= 2 AND chg24 < 0.05
"""

# 涨异动 × 当日 ≥50%（短线做多最强档，用于重点复核）
C50_SQL = BASE + """
SELECT '涨异动×当日≥50%' AS bucket,
       COUNT(*) AS n,
       AVG(r24h) AS m, percentile_cont(0.5) WITHIN GROUP (ORDER BY r24h) AS med,
       AVG((r24h > 0)::int) AS win,
       AVG(r24h - btc_r24) AS m_net,
       AVG((r24h > btc_r24)::int) AS win_net,
       COUNT(*) FILTER (WHERE rn = 1) AS n_indep,
       AVG(r24h) FILTER (WHERE rn = 1) AS m_indep,
       AVG((r24h > 0)::int) FILTER (WHERE rn = 1) AS win_indep,
       AVG((r24h > 0.001)::int) AS win_c1, AVG((r24h > 0.003)::int) AS win_c3,
       AVG(r24h - 0.001) AS m_c1, AVG(r24h - 0.003) AS m_c3
FROM x
WHERE chg1h >= 0.03 AND vr >= 2 AND chg24 >= 0.50
"""

BUCKET_EXPR = ("CASE WHEN chg24 < 0.05 THEN 'C·<5%' WHEN chg24 < 0.20 THEN 'C·+5~20%' "
               "WHEN chg24 < 0.50 THEN 'C·+20~50%' ELSE 'C·+50%+' END")

COLS = ["section", "bucket", "n", "win", "mean", "med",
        "n_net", "win_net", "mean_net",
        "n_indep", "win_indep", "mean_indep",
        "win_c1", "mean_c1", "win_c3", "mean_c3"]


def run(cur, section, sql, bucket_expr=None):
    cur.execute(sql.format(bucket_expr=bucket_expr or BUCKET_EXPR))
    rows = cur.fetchall()
    out = []
    for r in rows:
        row = {
            "section": section, "bucket": r[0], "n": r[1],
            "win": float(r[4] or 0), "mean": float(r[2] or 0), "med": float(r[3] or 0),
            "n_net": r[1], "win_net": float(r[6] or 0), "mean_net": float(r[5] or 0),
            "n_indep": int(r[7] or 0), "win_indep": float(r[9] or 0), "mean_indep": float(r[8] or 0),
            "win_c1": float(r[10] or 0), "mean_c1": float(r[12] or 0),
            "win_c3": float(r[11] or 0), "mean_c3": float(r[13] or 0),
        }
        out.append(row)
    return out


def show(title, rows, key_short, key_mid):
    print(f"\n== {title} ==")
    print(f"{'子集':<16}{'n':>7}{'胜%':>6}{'期望%':>8}{'中位%':>7} | "
          f"{'净alpha胜%':>9}{'净alpha%':>8} | {'独立n':>7}{'独立期望%':>8} | "
          f"{'成0.1胜%':>7}{'成0.3胜%':>7}")
    print("-" * 100)
    for r in rows:
        print(f"{r['bucket']:<16}{r['n']:>7,}{r['win']*100:>6.1f}{r['mean']*100:>8.2f}{r['med']*100:>7.2f} | "
              f"{r['win_net']*100:>9.1f}{r['mean_net']*100:>8.2f} | {r['n_indep']:>7,}{r['mean_indep']*100:>8.2f} | "
              f"{r['win_c1']*100:>7.1f}{r['win_c3']*100:>7.1f}")


def main() -> int:
    settings = get_settings(require_database=True)
    results = []
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            results += run(cur, "C", C_SQL)
            results += run(cur, "E", E_SQL)
            results += run(cur, "TRAP", TRAP_SQL, bucket_expr="'诱多做空' AS bucket")
            results += run(cur, "C50", C50_SQL)

    show("C 表 · 涨异动×当日涨幅分档 → 做多 T+24h（加固复核）", [r for r in results if r["section"] == "C"], None, None)
    show("E 表 · 涨幅≥30% × 有无异动 → 做多 T+24h", [r for r in results if r["section"] == "E"], None, None)
    show("诱多做空 · 未上榜放量大阳 → 做空 T+24h", [r for r in results if r["section"] == "TRAP"], None, None)
    show("重点复核 · 涨异动×当日≥50% 做多 T+24h", [r for r in results if r["section"] == "C50"], None, None)

    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\n结果已存 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
