#!/usr/bin/env python3
"""盘面异动扫描方案参数 × 涨幅榜 结合回测（2023~2026 周期，529 合约 1h）。

把《盘面异动扫描系统设计方案》的 L1 参数（§4.2/§8 单周期回测口径）叠加到
涨幅榜多空研究上，并跨周期复现方案的阈值扫描：

L1 回测口径（与方案一致）：1h 单根 `|chg1h| ≥ 价格阈值` 且 `量比 ≥ 量比阈值`（量比=当前
1h 成交额 / 近 20 根 1h 均值）。触发后 T+1h / T+24h 收益（做多=持有，做空=取负，毛）。

输出：
  A. 阈值扫描网格（价格 2/2.5/3/3.5/4.5/6% × 量比 1.5/2/3/4）→ 做多 24h 净均/胜率/PF
     （跨周期复现方案 §8 表，验证其 21 天样本内结论是否稳健）
  B. 涨跌方向 × 多空（涨异动 vs 跌异动）
  C. 事件日 24h 涨幅（涨幅榜）分档 × 多空 —— 结合本研究的涨幅榜维度
  D. 事件日资金费率 × 多空 —— 对比方案 §8「P↑OI↑+负费率 +15.6%」

用法：
    python backtest_scan_params_combined.py
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

MIN_N = 30
PRICE_GRID = (2.0, 2.5, 3.0, 3.5, 4.5, 6.0)
VR_GRID = (1.5, 2.0, 3.0, 4.0)
DATA_DIR = SCRIPT_DIR.parent / "data"

# 事件 = 1h 单根价量异动（最松网格口径，Python 端再按阈值过滤）
# T+1h / T+24h 用 LEAD 窗口一次算好（1h bar 计数），避免事件×24根 join（实测太重）。
# 回传：chg1h / vr / r1h / r24h / 事件日24h涨幅 / 当日费率
SQL = """
WITH volbase AS (
    SELECT symbol, open_time, close_px,
           close_px / open_px - 1 AS chg1h,
           quote_vol,
           AVG(quote_vol) OVER (PARTITION BY symbol ORDER BY open_time
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg20,
           LEAD(close_px, 1) OVER (PARTITION BY symbol ORDER BY open_time) AS px1h,
           LEAD(close_px, 24) OVER (PARTITION BY symbol ORDER BY open_time) AS px24h
    FROM biz.asset_klines
    WHERE interval = '1h' AND open_px > 0 AND close_px > 0
),
evt AS (
    SELECT symbol, open_time, close_px AS px0, chg1h,
           quote_vol / avg20 AS vr,
           px1h / close_px - 1 AS r1h,
           px24h / close_px - 1 AS r24h
    FROM volbase
    WHERE avg20 > 0 AND quote_vol / avg20 >= 1.5 AND abs(chg1h) >= 0.015
      AND px24h IS NOT NULL
),
daily AS (
    SELECT DISTINCT ON (symbol, DATE(open_time))
           symbol, DATE(open_time) AS d, close_px
    FROM biz.asset_klines
    WHERE interval = '1h' AND close_px > 0
    ORDER BY symbol, DATE(open_time), open_time DESC
),
dchg AS (
    SELECT s.symbol, s.d, s.close_px / p.close_px - 1 AS chg24
    FROM daily s JOIN daily p ON p.symbol = s.symbol AND p.d = s.d - 1
),
fund AS (
    SELECT DISTINCT ON (symbol, DATE(funding_time))
           symbol, DATE(funding_time) AS fd, rate
    FROM biz.funding_rate_hist
    ORDER BY symbol, DATE(funding_time), funding_time DESC
)
SELECT e.symbol, e.open_time AS eo, e.chg1h, e.vr, e.r1h, e.r24h,
       d.chg24, f.rate
FROM evt e
LEFT JOIN dchg d ON d.symbol = e.symbol AND d.d = DATE(e.open_time)
LEFT JOIN fund f ON f.symbol = e.symbol AND f.fd = DATE(e.open_time)
"""


def agg(xs: list[float]):
    n = len(xs)
    win = sum(1 for x in xs if x > 0) / n
    mean = statistics.fmean(xs)
    med = statistics.median(xs)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x <= 0]
    pf = (sum(wins) / -sum(losses) if wins and losses and sum(losses) != 0 else None)
    return n, win, mean, med, pf


def pct(x):
    return "—" if x is None else f"{x*100:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="盘面扫描参数 × 涨幅榜 结合回测")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 事件（冒烟）")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, rr)) for rr in cur.fetchall()]
    if args.limit:
        rows = rows[: args.limit]
    print(f"[events] 价量异动事件 {len(rows):,} 条（最松网格 vr≥1.5 & |chg1h|≥1.5%）\n")

    # A. 阈值扫描网格（复现方案 §8：做多 24h 净均；方案 21 天样本内，本回测 3.8 年）
    print("=== A. 阈值扫描网格（做多 T+24h；价格% × 量比；复现方案 §8 表） ===")
    print(f"{'价格\\量比':>10}" + "".join(f"{f'vr={v}':>16}" for v in VR_GRID))
    for p in PRICE_GRID:
        line = f"{p:>8}%"
        for v in VR_GRID:
            sub = [r for r in rows if r["chg1h"] >= p / 100 and r["vr"] >= v]
            if len(sub) < MIN_N:
                line += f"{'—':>16}"
                continue
            n, win, mean, med, pf = agg([r["r24h"] for r in sub])
            line += f"{mean*100:>7.2f}%({n})".rjust(16)
        print(line)

    # A2. 同网格做空（跌异动事件 |chg1h|≥p 且 vr≥v）
    print("\n=== A2. 阈值扫描网格（做空 T+24h；跌异动 |chg1h|≥p × vr≥v） ===")
    print(f"{'价格\\量比':>10}" + "".join(f"{f'vr={v}':>16}" for v in VR_GRID))
    for p in PRICE_GRID:
        line = f"{p:>8}%"
        for v in VR_GRID:
            sub = [r for r in rows if r["chg1h"] <= -p / 100 and r["vr"] >= v]
            if len(sub) < MIN_N:
                line += f"{'—':>16}"
                continue
            n, win, mean, med, pf = agg([-r["r24h"] for r in sub])
            line += f"{mean*100:>7.2f}%({n})".rjust(16)
        print(line)

    # B. 涨/跌异动 × 多空（T+1h / T+24h）
    print("\n=== B. 方向 × 多空（方案 L1 线上口径：|chg1h|≥3% & vr≥2） ===")
    for lab, cond in (("涨异动(+3%&vr2)", lambda r: r["chg1h"] >= 0.03 and r["vr"] >= 2),
                      ("跌异动(-3%&vr2)", lambda r: r["chg1h"] <= -0.03 and r["vr"] >= 2)):
        sub = [r for r in rows if cond(r)]
        if len(sub) < MIN_N:
            print(f"{lab:>14} 样本不足")
            continue
        n, lw, lm, lmed, lpf = agg([r["r24h"] for r in sub])
        _, sw, sm, smed, spf = agg([-r["r24h"] for r in sub])
        n1, _, lm1, _, _ = agg([r["r1h"] for r in sub])
        print(f"{lab:>14} n={n:>6} | 多T+1h {pct(lm1)}% | 多T+24h {pct(lm)}%(胜{lw*100:.0f}%) PF={lpf and round(lpf,2)} | "
              f"空T+24h {pct(sm)}%(胜{sw*100:.0f}%) 中位{pct(smed)}")

    # C. 事件日 24h 涨幅（涨幅榜结合）分档 × 做多/做空 T+24h（涨异动事件）
    print("\n=== C. 涨异动事件 × 当日涨幅榜分档 × 多空（T+24h） ===")
    up = [r for r in rows if r["chg1h"] >= 0.03 and r["vr"] >= 2 and r["chg24"] is not None]
    print(f"{'当日24h涨幅':>14} {'n':>7} | {'多胜%':>6} {'多期望%':>8} {'多中位%':>7} | {'空胜%':>6} {'空期望%':>8} {'空中位%':>7}")
    print("-" * 78)
    for lo, hi, lab in ((0, 5, "<5%(未上榜)"), (5, 20, "+5~20%"), (20, 50, "+20~50%"), (50, 1e9, "+50%+"), (0, 1e9, "全部")):
        sub = [r for r in up if (r["chg24"] * 100) >= lo and (r["chg24"] * 100) < hi]
        if len(sub) < MIN_N:
            continue
        n, lw, lm, lmed, _ = agg([r["r24h"] for r in sub])
        _, sw, sm, smed, _ = agg([-r["r24h"] for r in sub])
        print(f"{lab:>14} {n:>7} | {lw*100:>6.1f} {pct(lm):>8} {pct(lmed):>7} | {sw*100:>6.1f} {pct(sm):>8} {pct(smed):>7}")

    # D. 事件日费率 × 多空（对比方案 §8「P↑OI↑+负费率」）
    print("\n=== D. 涨异动事件 × 当日费率 × 多空（T+24h） ===")
    print(f"{'费率档':>10} {'n':>7} | {'多胜%':>6} {'多期望%':>8} {'多中位%':>7} | {'空胜%':>6} {'空期望%':>8} {'空中位%':>7}")
    print("-" * 78)
    for lab, cond in (("费率正高(≥5bp)", lambda r: r["rate"] is not None and r["rate"] >= 0.0005),
                      ("费率正", lambda r: r["rate"] is not None and 0.0001 <= r["rate"] < 0.0005),
                      ("费率近零", lambda r: r["rate"] is not None and -0.0001 < r["rate"] < 0.0001),
                      ("费率负", lambda r: r["rate"] is not None and r["rate"] <= -0.0001)):
        sub = [r for r in up if cond(r)]
        if len(sub) < MIN_N:
            print(f"{lab:>10} 样本不足")
            continue
        n, lw, lm, lmed, _ = agg([r["r24h"] for r in sub])
        _, sw, sm, smed, _ = agg([-r["r24h"] for r in sub])
        print(f"{lab:>10} {n:>7} | {lw*100:>6.1f} {pct(lm):>8} {pct(lmed):>7} | {sw*100:>6.1f} {pct(sm):>8} {pct(smed):>7}")

    # E. 与涨幅榜研究的核心组合对比：涨幅榜≥50% 信号日是否叠加 1h 异动
    print("\n=== E. 涨幅榜≥30% 信号 × 当日有无 1h 异动（对比多空，T+24h） ===")
    hi_evt = [r for r in rows if r["chg24"] is not None and r["chg24"] * 100 >= 30]
    print(f"{'子集':>22} {'n':>7} | {'多胜%':>6} {'多期望%':>8} | {'空胜%':>6} {'空期望%':>8} {'空中位%':>7}")
    for lab, cond in (("涨幅≥30% & 有涨异动", lambda r: r["chg1h"] >= 0.03 and r["vr"] >= 2),
                      ("涨幅≥30% & 有跌异动", lambda r: r["chg1h"] <= -0.03 and r["vr"] >= 2),
                      ("涨幅≥30% & 无异动", lambda r: abs(r["chg1h"]) < 0.03 or r["vr"] < 2),
                      ("涨幅≥30% & 有涨异动&费率负", lambda r: r["chg1h"] >= 0.03 and r["vr"] >= 2
                       and r["rate"] is not None and r["rate"] <= -0.0001)):
        sub = [r for r in hi_evt if cond(r)]
        if len(sub) < MIN_N:
            print(f"{lab:>22} 样本不足")
            continue
        n, lw, lm, _, _ = agg([r["r24h"] for r in sub])
        _, sw, sm, smed, _ = agg([-r["r24h"] for r in sub])
        print(f"{lab:>22} {n:>7} | {lw*100:>6.1f} {pct(lm):>8} | {sw*100:>6.1f} {pct(sm):>8} {pct(smed):>7}")

    # 写 CSV（网格 + 分桶）
    out_csv = DATA_DIR / "backtest_scan_params_combined.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "label", "p_thr", "vr_thr", "n", "win", "mean", "med", "pf"])
        for p in PRICE_GRID:
            for v in VR_GRID:
                for side, key, flip in (("long_up", lambda r: r["chg1h"] >= p / 100 and r["vr"] >= v, 1),
                                        ("short_down", lambda r: r["chg1h"] <= -p / 100 and r["vr"] >= v, -1)):
                    sub = [r for r in rows if key(r)]
                    if len(sub) >= MIN_N:
                        n, win, mean, med, pf = agg([flip * r["r24h"] for r in sub])
                        w.writerow(["grid", side, p, v, n, round(win, 4), round(mean, 4), round(med, 4),
                                    round(pf, 3) if pf else ""])
    print(f"\n结果已存 {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
