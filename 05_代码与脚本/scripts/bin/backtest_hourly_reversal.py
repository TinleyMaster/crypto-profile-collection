#!/usr/bin/env python3
"""小时级反转信号回测（Binance USDT 永续 1h K 线）。

背景（2026-10-03）：涨幅榜研究已确认日线维度「24h 涨幅 ≥75% 后 7 天大概率回落」，
但用户追问小时级是否存在可择时的反转信号。本脚本对四种经典"超买/反转"信号做小时级回测：

  F1 单根大阳线     —— 单根 1h 涨幅 ≥5%
  F2 RSI(14) ≥85   —— 小时级 RSI 超买
  F3 黄昏星         —— 大阳(≥2%) → 小实体(≤1.5%) → 大阴跌破前根实体中点
  F4 长上影线       —— 阳线且上影线 ≥N× 实体

三个视角：
  A. 全样本 vs 24h≥75% 极端子样本（"涨 75%+ 后的小时级信号是否反而预示转跌"）
  B. 市值分档（<300M / 300M~1B / >1B，市值取 CMC 快照映射，币安永续的相对小/中/大）
  C. 后续窗口 1/4/12/24h 跌概率 + 中位收益

判据：后续跌概率 vs 全市场基准（跌 12h ≈ 48~50%）。

⚠️ 已知结论（provisional）：
  - 全样本：F1 无预测力、F2 反向（动量延续）、F3/F4 无效；
  - ≥75% 极端子样本：F1~F4 全部偏继续涨（无小时级反转信号）；
  - 市值视角：仅 F1 在 <300M 小市值有效（跌 12h 58.6% vs 基准 48.2%）；
    F2 在所有市值档都是动量延续。
  - 市值映射：CMC 快照每日市值 → cmc_asset_map → 币安 symbol；映射良好但覆盖非全。
  - 样本 ~3.5 个月单一 regime；≥75% 子样本仅 38 事件，结论谨慎。

用法：
    python backtest_hourly_reversal.py
    python backtest_hourly_reversal.py --min-n 30 --out data/backtest_hourly_reversal.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

MIN_N = 30                      # 统计最少样本
GE75_THR = 0.75                 # "24h≥75%" 事件阈值
MCAP_BANDS = ((0, 300e6, "S<300M"), (300e6, 1e9, "M300M~1B"), (1e9, 1e30, "L>1B"))

# 共享 CTE 前缀（信号四段用 s 全量 bar；市值 join 只属于 mcap 段，避免过滤未映射币）
_CTE = """
WITH bars AS (
    SELECT symbol, open_time, open_px, close_px, high_px,
           close_px / LAG(close_px) OVER w - 1 AS chg,
           (high_px - GREATEST(open_px, close_px)) / NULLIF(ABS(close_px - open_px), 0) AS uwr,
           LEAD(close_px,1)  OVER w / close_px - 1 AS r1,
           LEAD(close_px,4)  OVER w / close_px - 1 AS r4,
           LEAD(close_px,12) OVER w / close_px - 1 AS r12,
           LEAD(close_px,24) OVER w / close_px - 1 AS r24,
           DATE(open_time) AS d
    FROM biz.asset_klines WHERE interval='1h' AND open_px>0 AND close_px>0 AND high_px>0
    WINDOW w AS (PARTITION BY symbol ORDER BY open_time)
),
g AS (
    SELECT *,
           AVG(GREATEST(chg,0)) OVER (PARTITION BY symbol ORDER BY open_time
                ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS gain,
           AVG(GREATEST(-chg,0)) OVER (PARTITION BY symbol ORDER BY open_time
                ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS loss,
           LAG(open_px) OVER w AS o1, LAG(close_px) OVER w AS c1,
           LAG(open_px,2) OVER w AS o0, LAG(close_px,2) OVER w AS c0
    FROM bars WINDOW w AS (PARTITION BY symbol ORDER BY open_time)
),
s AS (
    SELECT *, CASE WHEN loss = 0 THEN 100.0 ELSE 100 - 100 / (1 + gain / loss) END AS rsi14,
           (close_px < open_px AND c0 > o0 AND (c0 - o0) / o0 >= 0.02
            AND ABS(c1 - o1) <= ABS(c0 - o0) * 0.5 AND ABS(c1 - o1) <= o0 * 0.015
            AND close_px <= (o0 + c0) / 2.0) AS is_star
    FROM g WHERE chg IS NOT NULL
),
daily AS (
    SELECT DISTINCT ON (symbol, DATE(open_time)) symbol, DATE(open_time) AS d, close_px
    FROM biz.asset_klines WHERE interval='1h' AND close_px > 0
    ORDER BY symbol, DATE(open_time), open_time DESC
),
ev AS (
    SELECT a.symbol, a.d FROM daily a JOIN daily p ON p.symbol = a.symbol AND p.d = a.d - 1
    WHERE a.close_px / p.close_px - 1 >= %s
)
"""

# 单根 1h 涨幅分桶 × in75（s 全量 bar）
Q_CHG = _CTE + """
SELECT 'chg' AS section,
       CASE WHEN chg < 0.01 THEN '0~1' WHEN chg < 0.02 THEN '1~2' WHEN chg < 0.03 THEN '2~3'
            WHEN chg < 0.05 THEN '3~5' WHEN chg < 0.08 THEN '5~8' WHEN chg < 0.12 THEN '8~12'
            WHEN chg < 0.20 THEN '12~20' ELSE '20+' END AS label,
       (e.symbol IS NOT NULL) AS in75,
       COUNT(*) AS n,
       AVG((r4 < 0)::int) AS p4, AVG((r12 < 0)::int) AS p12, AVG((r24 < 0)::int) AS p24,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r12) AS m12,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r24) AS m24
FROM s LEFT JOIN ev e ON e.symbol = s.symbol AND s.d BETWEEN e.d AND e.d + 2
GROUP BY 2, 3
"""

# 1h RSI 分档 × in75
Q_RSI = _CTE + """
SELECT 'rsi' AS section,
       CASE WHEN rsi14 < 60 THEN 'RSI<60' WHEN rsi14 < 70 THEN '60~70' WHEN rsi14 < 80 THEN '70~80'
            WHEN rsi14 < 85 THEN '80~85' WHEN rsi14 < 90 THEN '85~90' WHEN rsi14 < 95 THEN '90~95'
            ELSE '95+' END AS label,
       (e.symbol IS NOT NULL) AS in75,
       COUNT(*) AS n,
       AVG((r4 < 0)::int) AS p4, AVG((r12 < 0)::int) AS p12, AVG((r24 < 0)::int) AS p24,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r12) AS m12,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r24) AS m24
FROM s LEFT JOIN ev e ON e.symbol = s.symbol AND s.d BETWEEN e.d AND e.d + 2
GROUP BY 2, 3
"""

# 黄昏星 × in75
Q_STAR = _CTE + """
SELECT 'star' AS section, '黄昏星' AS label,
       (e.symbol IS NOT NULL) AS in75,
       COUNT(*) AS n,
       AVG((r4 < 0)::int) AS p4, AVG((r12 < 0)::int) AS p12, AVG((r24 < 0)::int) AS p24,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r12) AS m12,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r24) AS m24
FROM s LEFT JOIN ev e ON e.symbol = s.symbol AND s.d BETWEEN e.d AND e.d + 2
WHERE s.is_star
GROUP BY 2, 3
"""

# 长上影线（上影 ≥ N×实体，阳线） × in75
Q_WICK = _CTE + """
SELECT 'wick' AS section, w.k AS label,
       (e.symbol IS NOT NULL) AS in75,
       COUNT(*) AS n,
       AVG((r4 < 0)::int) AS p4, AVG((r12 < 0)::int) AS p12, AVG((r24 < 0)::int) AS p24,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r12) AS m12,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r24) AS m24
FROM s LEFT JOIN ev e ON e.symbol = s.symbol AND s.d BETWEEN e.d AND e.d + 2
CROSS JOIN (VALUES (0),(1),(2),(3),(5)) AS w(k)
WHERE s.close_px > s.open_px AND (w.k = 0 OR s.uwr >= w.k)
GROUP BY 2, 3
"""

# 市值分档 × 四种因子（全样本；市值 join 仅此段）
Q_MCAP = _CTE + """
, mcap AS (
    SELECT q.cmc_id, DATE(q.quote_time) AS d,
           (ARRAY_AGG(q.market_cap ORDER BY q.quote_time DESC))[1] AS mc
    FROM src_cmc.cmc_asset_quote_snapshot q WHERE q.market_cap > 0
    GROUP BY q.cmc_id, DATE(q.quote_time)
),
m AS (
    SELECT UPPER(REPLACE(symbol, 'USDT', '')) AS base, cmc_id
    FROM src_cmc.cmc_asset_map WHERE symbol IS NOT NULL
),
j AS (
    SELECT s.*, mcap.mc
    FROM s JOIN m ON m.base = UPPER(REPLACE(s.symbol, 'USDT', ''))
    LEFT JOIN mcap ON mcap.cmc_id = m.cmc_id AND mcap.d = s.d
)
SELECT 'mcap' AS section,
       CASE WHEN mc < 300000000 THEN 'S<300M' WHEN mc < 1000000000 THEN 'M300M~1B' ELSE 'L>1B' END AS band,
       f.f AS label,
       COUNT(*) AS n,
       AVG((r4 < 0)::int) AS p4, AVG((r12 < 0)::int) AS p12, AVG((r24 < 0)::int) AS p24,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r12) AS m12,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r24) AS m24
FROM j
CROSS JOIN (VALUES ('all'), ('big5'), ('rsi85'), ('star'), ('wick3')) AS f(f)
WHERE j.mc IS NOT NULL
  AND ((f.f = 'all' AND j.close_px > j.open_px)
    OR (f.f = 'big5' AND j.chg >= 0.05)
    OR (f.f = 'rsi85' AND j.rsi14 >= 85)
    OR (f.f = 'star' AND j.is_star)
    OR (f.f = 'wick3' AND j.close_px > j.open_px AND j.uwr >= 3))
GROUP BY 2, 3
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="小时级反转信号回测（币安永续 1h）")
    parser.add_argument("--min-n", type=int, default=MIN_N, help="统计最少样本")
    parser.add_argument("--out", type=str, default="", help="CSV 输出路径")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    all_rows: list[tuple] = []
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            for q in (Q_CHG, Q_RSI, Q_STAR, Q_WICK, Q_MCAP):
                cur.execute(q, (GE75_THR,))
                all_rows.extend(cur.fetchall())

    # 按 section 分组打印
    def _print_sec(name: str, title: str, label_hdr: str, scope_hdr: bool) -> None:
        rows = [r for r in all_rows if r[0] == name]
        rows = [r for r in rows if r[3] >= args.min_n]
        if not rows:
            return
        print(f"\n=== {title} ===")
        cols = [("all", "全样本"), ("ge75", "24h≥75%")] if scope_hdr else [("all", "全样本")]
        hdr = f"{label_hdr:>12}"
        for _, h in cols:
            hdr += f"{h + '跌12h':>12}{h + '跌24h':>12}{h + '中位12h':>12}"
        print(hdr)
        print("-" * len(hdr))
        labels = sorted({r[1] for r in rows}, key=lambda x: (str(x).isdigit(), str(x)))
        for label in labels:
            line = f"{label:>12}"
            for scope, _h in cols:
                sub = [rr for rr in rows
                       if rr[1] == label and (rr[2] is False) == (scope == "all")]
                if not sub:
                    line += f"{'--':>12}{'--':>12}{'--':>12}"
                    continue
                _, _, _, _, _, p12_, p24_, m12_, _ = sub[0]
                line += f"{p12_ * 100:>11.1f}%{p24_ * 100:>11.1f}%"
                line += f"{m12_ * 100 if m12_ is not None else 0:>11.2f}"
            print(line)

    _print_sec("chg", "单根 1h 涨幅分桶 → 后续跌概率（vs 全市场基准跌12h≈48~50%）", "单根涨幅", True)
    _print_sec("rsi", "1h RSI(14) 分档 → 后续跌概率", "RSI档", True)
    _print_sec("star", "黄昏星 → 后续跌概率", "形态", True)
    _print_sec("wick", "长上影线（上影≥N×实体）→ 后续跌概率", "上影/实体", True)

    # 市值 × 因子（行 = 市值档，列 = 因子）
    rows = [r for r in all_rows if r[0] == "mcap" and r[3] >= args.min_n]
    if rows:
        print("\n=== 市值分档 × 四种因子（全样本，跌12h% / n）===")
        print(f"{'市值档':>12}" + "".join(f"{h:>16}" for h in ["阳线基准", "单根涨5%", "RSI≥85", "黄昏星", "上影≥3倍"]))
        print("-" * (12 + 16 * 5))
        bands = ["S<300M", "M300M~1B", "L>1B"]
        facs = ["all", "big5", "rsi85", "star", "wick3"]
        for band in bands:
            line = f"{band:>12}"
            for f in facs:
                sub = [r for r in rows if r[1] == band and r[2] == f]
                if not sub:
                    line += f"{'--':>16}"
                    continue
                _, _, _, n, _, p12, _, m12, _ = sub[0]
                line += f"{p12 * 100:>10.1f}%/{n:<5}"
            print(line)

    # CSV
    if not args.out:
        args.out = str(SCRIPT_DIR.parent / "data" / "backtest_hourly_reversal.csv")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "label", "scope", "n", "p4h", "p12h", "p24h", "m12h", "m24h"])
        for sec, label, in75, n, p4, p12, p24, m12, m24 in all_rows:
            if n < args.min_n:
                continue
            scope = "ge75" if in75 else "all"
            w.writerow([sec, label, scope, n, p4, p12, p24, m12, m24])
    print(f"\n[hourly] 结果已存 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
