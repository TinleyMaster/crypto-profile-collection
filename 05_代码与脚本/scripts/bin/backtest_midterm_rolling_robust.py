#!/usr/bin/env python3
"""中线（T+7d）与短线（T+24h）规则 · **bar 级滚动 24h 口径**重跑 + 三口径加固。

背景：`backtest_binance_long_short.py` 是**日线粒度**策略（信号=当日末收盘涨幅、
入场=当日末收盘），无法产出 bar 级（事件当下入场）版本，且其 chg24 用「当日末/前一日末」
日终口径，与本方案 bar 级滚动 24h（对齐线上 `ticker/24hr`）不可直接比较。

本脚本把 §2.3/§2.4（T+7d）与 §2.1（T+24h）的规则搬到 **bar 级滚动 24h** 口径复核：
  S0 做多 chg24 ≥50% 且放量大阳（1h≥3% & vr≥2）           —— §2.1 A 信号（T+24h）
  S1 做空 chg24 ≥75%（极端涨幅）                            —— §2.3 规则1（T+168h）
  S2 做空 chg24 ≥30% 且 chg1h ≥20%（巨阳）                  —— §2.3 规则2（T+168h）
  S3 做多 chg24 ∈[5%,20%) 且 chg1h <10% 且市值 ≥3e8         —— §2.4（T+168h）

对每条规则同时给出 §7 三口径加固列：
  - 扣 BTC β（β=1 简单超额，按方向调整符号）
  - 独立事件去重（每币每日首事件）
  - 成本敏感性（往返 0.1% / 0.3%）

口径：chg24 = close_px / LAG(close_px,24) - 1（事件 bar 收盘 / 24h 前 bar 收盘）；
      r24h = LEAD(close_px,24)/close_px - 1；r168h = LEAD(close_px,168)/close_px - 1。
⚠️ LAG/LEAD 按 bar 序号（24 / 168 根）计，密集合约（1h 连续）下等价于 24h / 168h。

服务端全量聚合、只回传少量汇总行（避免逐行下载卡尾）。
用法：python bin/backtest_midterm_rolling_robust.py
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
OUT = DATA_DIR / "backtest_midterm_rolling_robust.csv"

MC_MIN = 3e8          # 大市值门槛（USD）—— §2.4
COST_1, COST_3 = 0.001, 0.003

SQL = """
WITH volbase AS (
    SELECT symbol, open_time, close_px,
           close_px / open_px - 1 AS chg1h,
           quote_vol,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20,
           close_px / LAG(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) - 1 AS chg24,
           LEAD(close_px, 24)  OVER (PARTITION BY symbol ORDER BY open_time) AS px24h,
           LEAD(close_px, 168) OVER (PARTITION BY symbol ORDER BY open_time) AS px168h
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0
),
evt AS (
    SELECT symbol, open_time, chg1h, chg24,
           quote_vol / avg20 AS vr,
           px24h  / close_px - 1 AS r24h,
           px168h / close_px - 1 AS r168h
    FROM volbase
    WHERE chg24 IS NOT NULL
),
btc AS (
    SELECT open_time,
           LEAD(close_px, 24)  OVER (ORDER BY open_time) / close_px - 1 AS btc_r24,
           LEAD(close_px, 168) OVER (ORDER BY open_time) / close_px - 1 AS btc_r168
    FROM biz.asset_klines
    WHERE interval = '1h' AND symbol = 'BTCUSDT' AND open_px > 0
),
amap AS (
    -- 每个 base 取唯一 cmc_id，避免 1:N 映射把事件行数放大
    SELECT DISTINCT ON (UPPER(REPLACE(symbol, 'USDT', '')))
           UPPER(REPLACE(symbol, 'USDT', '')) AS base, cmc_id
    FROM src_cmc.cmc_asset_map WHERE symbol IS NOT NULL
    ORDER BY UPPER(REPLACE(symbol, 'USDT', '')), cmc_id
),
mcap AS (
    SELECT DISTINCT ON (cmc_id, DATE(quote_time)) cmc_id, DATE(quote_time) AS d, market_cap AS mc
    FROM src_cmc.cmc_asset_quote_snapshot
    WHERE market_cap > 0
    ORDER BY cmc_id, DATE(quote_time), quote_time DESC
),
base AS (
    SELECT e.symbol, e.open_time, e.chg1h, e.chg24, e.vr, e.r24h, e.r168h,
           b.btc_r24, b.btc_r168, c.mc,
           ROW_NUMBER() OVER (PARTITION BY e.symbol, DATE(e.open_time)
                              ORDER BY e.open_time) AS rn
    FROM evt e
    LEFT JOIN btc  b ON b.open_time = e.open_time
    LEFT JOIN amap m ON m.base = UPPER(REPLACE(e.symbol, 'USDT', ''))
    LEFT JOIN mcap c ON c.cmc_id = m.cmc_id AND c.d = DATE(e.open_time)
),
u AS (
    -- S0 short-term momentum long, T+24h
    SELECT 'S0_long_momentum_24h' AS rule, 1 AS dir, r24h AS ret,
           symbol, open_time, btc_r24 AS btc_r, rn
    FROM base
    WHERE chg24 >= 0.50 AND chg1h >= 0.03 AND vr >= 2 AND r24h IS NOT NULL
    UNION ALL
    -- S1 mid-term extreme short, chg24 ge 0.75, T+168h
    SELECT 'S1_short_ge75_168h', -1, -r168h, symbol, open_time, btc_r168, rn
    FROM base WHERE chg24 >= 0.75 AND r168h IS NOT NULL
    UNION ALL
    -- S2 mid-term giant-bar short, chg24 ge 0.30 and chg1h ge 0.20, T+168h
    SELECT 'S2_short_giant30_168h', -1, -r168h, symbol, open_time, btc_r168, rn
    FROM base WHERE chg24 >= 0.30 AND chg1h >= 0.20 AND r168h IS NOT NULL
    UNION ALL
    -- S3 mid-term mild long, chg24 in [0.05, 0.20), chg1h lt 0.10, mcap ge 3e8, T+168h
    SELECT 'S3_long_mild_168h', 1, r168h, symbol, open_time, btc_r168, rn
    FROM base
    WHERE chg24 >= 0.05 AND chg24 < 0.20 AND chg1h < 0.10
      AND mc >= %s AND r168h IS NOT NULL
)
SELECT rule,
       COUNT(*) AS n,
       MIN(open_time)::date AS d0, MAX(open_time)::date AS d1,
       COUNT(DISTINCT DATE(open_time)) AS days,
       COUNT(DISTINCT symbol) AS coins,
       AVG((ret > 0)::int) AS win, AVG(ret) AS mean,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY ret) AS med,
       SUM(GREATEST(ret, 0)) / NULLIF(ABS(SUM(LEAST(ret, 0))), 0) AS pf,
       MIN(ret) AS worst,
       COUNT(btc_r) AS n_net,
       AVG(((ret - dir * btc_r) > 0)::int) AS win_net,
       AVG(ret - dir * btc_r) AS mean_net,
       COUNT(*) FILTER (WHERE rn = 1) AS n_indep,
       AVG((ret > 0)::int) FILTER (WHERE rn = 1) AS win_indep,
       AVG(ret) FILTER (WHERE rn = 1) AS mean_indep,
       AVG((ret > %s)::int) AS win_c1, AVG(ret - %s) AS mean_c1,
       AVG((ret > %s)::int) AS win_c3, AVG(ret - %s) AS mean_c3
FROM u
GROUP BY rule
ORDER BY rule
"""

COLS = ["rule", "n", "d0", "d1", "days", "coins", "win", "mean", "med", "pf", "worst",
        "n_net", "win_net", "mean_net", "n_indep", "win_indep", "mean_indep",
        "win_c1", "mean_c1", "win_c3", "mean_c3"]


def pct(x, nd=2):
    return "—" if x is None else f"{float(x) * 100:.{nd}f}"


def main() -> int:
    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, (MC_MIN, COST_1, COST_1, COST_3, COST_3))
            rows = cur.fetchall()

    recs = []
    for r in rows:
        rec = dict(zip(COLS, r))
        for k in COLS:
            if k in ("rule", "d0", "d1"):
                continue
            rec[k] = None if rec[k] is None else float(rec[k])
        recs.append(rec)

    print("\n=== 规则 · bar 级滚动 24h 口径 · 三口径加固 ===")
    hdr = (f"{'规则':<24}{'n':>7}{'币':>5}{'区间(日)':>22}{'天数':>6}"
           f"{'胜%':>7}{'期望%':>8}{'中位%':>8}{'PF':>7}{'最差%':>8}"
           f"{'| 净β胜%':>9}{'净β期望%':>10}"
           f"{'| 独立n':>8}{'独立期望%':>10}"
           f"{'| 成0.1胜%':>9}{'成0.3胜%':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in recs:
        span = f"{r['d0']}~{r['d1']}"
        print(f"{r['rule']:<24}{int(r['n']):>7}{int(r['coins']):>5}{span:>22}{int(r['days']):>6}"
              f"{pct(r['win'],1):>7}{pct(r['mean']):>8}{pct(r['med']):>8}"
              f"{r['pf']:>7.2f}{pct(r['worst'],1):>8}"
              f"{pct(r['win_net'],1):>9}{pct(r['mean_net']):>10}"
              f"{int(r['n_indep']):>8}{pct(r['mean_indep']):>10}"
              f"{pct(r['win_c1'],1):>9}{pct(r['win_c3'],1):>9}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in recs:
            w.writerow(r)
    print(f"\n结果已存 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())