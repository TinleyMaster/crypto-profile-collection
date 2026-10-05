#!/usr/bin/env python3
"""币安涨幅榜 · 做多/做空条件矩阵回测（2023~2026 周期，529 合约）。

目标：找到可执行的做多、做空入场条件（胜率/期望/中位/盈亏比）。
对每条涨幅榜信号（chg24 ≥5%）同时评估两个方向（毛收益，不计成本/资金费率）：
  - 做多 = 持有 r 日收益         胜率 = P(ret>0)，期望 = AVG(ret)
  - 做空 = -持有收益            胜率 = P(ret<0)，期望 = AVG(-ret)
因子分桶（基于前期研究确认的因子）：
  - 涨幅档：5-10/10-20/20-30/30-50/50-75/75+
  - 市值档：S<300M / M300M~1B / L>1B（CMC 快照映射）
  - 当日单根 1h 巨阳 max1h：<10 / 10-20 / 20-40 / 40+（脉冲拉高）
  - 放量分档：信号日 24h 成交额四分位
  - 资金费率：正高/正/近零/负（已证 regime 依赖，仅参考）

用法：
    python backtest_binance_long_short.py
    python backtest_binance_long_short.py --min-gain 5 --horizon 7
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

MIN_GAIN = 5.0
MIN_N = 30            # 分桶最少样本
DATA_DIR = SCRIPT_DIR.parent / "data"

# 信号级回传：chg(24h涨幅)/vol(成交额)/max1h(当日单根最大1h涨幅)/rate(费率)/mc(市值)
# + 各窗口持有收益（做多），做空取其负。
SQL = """
WITH daily AS (
    SELECT DISTINCT ON (symbol, DATE(open_time))
           symbol, DATE(open_time) AS d, close_px, quote_vol
    FROM biz.asset_klines
    WHERE interval = '1h' AND close_px > 0
    ORDER BY symbol, DATE(open_time), open_time DESC
),
sig AS (
    SELECT s.symbol, s.d, s.close_px AS entry, s.quote_vol AS vol,
           s.close_px / p.close_px - 1 AS chg
    FROM daily s
    JOIN daily p ON p.symbol = s.symbol AND p.d = s.d - 1
    WHERE s.close_px / p.close_px - 1 >= %s
      AND s.d <= (SELECT MAX(d) FROM daily) - 14
),
fwd AS (
    SELECT s.symbol, s.d AS sig_d, s.entry, s.chg, s.vol,
           fk.close_px / s.entry - 1 AS ret, (fk.d - s.d) AS k
    FROM sig s JOIN daily fk ON fk.symbol = s.symbol AND fk.d > s.d AND fk.d <= s.d + 14
),
r AS (
    SELECT symbol, sig_d,
           MAX(ret) FILTER (WHERE k = 1) AS r1,
           MAX(ret) FILTER (WHERE k = 3) AS r3,
           MAX(ret) FILTER (WHERE k = 7) AS r7,
           MAX(ret) FILTER (WHERE k = 14) AS r14
    FROM fwd GROUP BY symbol, sig_d
),
g AS (
    SELECT symbol, DATE(open_time) AS d,
           MAX(close_px / open_px - 1) AS max1h
    FROM biz.asset_klines WHERE interval = '1h' AND open_px > 0
    GROUP BY symbol, DATE(open_time)
),
mcap AS (
    SELECT q.cmc_id, DATE(q.quote_time) AS d,
           (ARRAY_AGG(q.market_cap ORDER BY q.quote_time DESC))[1] AS mc
    FROM src_cmc.cmc_asset_quote_snapshot q WHERE q.market_cap > 0
    GROUP BY q.cmc_id, DATE(q.quote_time)
),
m AS (
    SELECT UPPER(REPLACE(symbol, 'USDT', '')) AS base, cmc_id
    FROM src_cmc.cmc_asset_map WHERE symbol IS NOT NULL
),
fund AS (
    SELECT DISTINCT ON (symbol, DATE(funding_time))
           symbol, DATE(funding_time) AS fd, rate
    FROM biz.funding_rate_hist
    ORDER BY symbol, DATE(funding_time), funding_time DESC
)
SELECT s.symbol, s.d AS sig_d, s.chg, s.vol, g.max1h, f.rate, c.mc,
       r.r1, r.r3, r.r7, r.r14
FROM sig s
JOIN r ON r.symbol = s.symbol AND r.sig_d = s.d
LEFT JOIN g ON g.symbol = s.symbol AND g.d = s.d
LEFT JOIN m ON m.base = UPPER(REPLACE(s.symbol, 'USDT', ''))
LEFT JOIN mcap c ON c.cmc_id = m.cmc_id AND c.d = s.d
LEFT JOIN fund f ON f.symbol = s.symbol AND f.fd = s.d
"""


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x*100:.1f}"


def band_stats(rows, get_key, h):
    """按因子键聚合多空统计。返回 {key: (n, 多空统计)}。"""
    groups: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        key = get_key(row)
        if key is None:
            continue
        r = row[f"r{h}"]
        if r is None:
            continue
        groups.setdefault(key, []).append((r, -r))
    out = {}
    for k, vs in groups.items():
        if len(vs) < MIN_N:
            continue
        longs = [a for a, _ in vs]
        shorts = [b for _, b in vs]
        n = len(vs)
        def agg(xs):
            win = sum(1 for x in xs if x > 0) / n
            mean = statistics.fmean(xs)
            med = statistics.median(xs)
            wins = [x for x in xs if x > 0]
            losses = [x for x in xs if x <= 0]
            pl = (statistics.fmean(wins) / -statistics.fmean(losses)
                  if wins and losses and statistics.fmean(losses) != 0 else None)
            return win, mean, med, pl
        out[k] = (n, agg(longs), agg(shorts))
    return out


def print_band(title, out, h):
    print(f"\n=== {title}（做多=持有{h}日；做空=-收益；毛、不计成本） ===")
    print(f"{'分组':>14} {'n':>7} | {'多胜%':>6} {'多期望%':>8} {'多中位%':>7} | "
          f"{'空胜%':>6} {'空期望%':>8} {'空中位%':>7} {'空盈亏比':>7}")
    print("-" * 78)
    for k in sorted(out, key=str):
        n, L, S = out[k]
        print(f"{str(k):>14} {n:>7} | {fmt_pct(L[0]):>6} {fmt_pct(L[1]):>8} {fmt_pct(L[2]):>7} | "
              f"{fmt_pct(S[0]):>6} {fmt_pct(S[1]):>8} {fmt_pct(S[2]):>7} "
              f"{(f'{S[3]:.2f}' if S[3] else '—'):>7}")


def main() -> int:
    ap = argparse.ArgumentParser(description="币安涨幅榜做多/做空条件矩阵回测")
    ap.add_argument("--min-gain", type=float, default=MIN_GAIN)
    ap.add_argument("--horizon", type=int, default=7, choices=(1, 3, 7, 14))
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条信号（冒烟）")
    args = ap.parse_args()
    h = args.horizon

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, (args.min_gain / 100,))
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, rr)) for rr in cur.fetchall()]
    if args.limit:
        rows = rows[: args.limit]
    print(f"[signals] {len(rows):,} 条（chg24 ≥{args.min_gain}%，窗口 {h} 日）")

    # 分位数（成交额四分位，用于放量分档）
    vols = sorted(r["vol"] for r in rows if r["vol"] and r["vol"] > 0)
    if vols:
        qs = [vols[int(len(vols) * q)] for q in (0.25, 0.5, 0.75)]
    else:
        qs = [0, 0, 0]

    # 1) 涨幅档
    def k_chg(r):
        c = r["chg"] * 100
        for hi, lab in ((10, "+5~10%"), (20, "+10~20%"), (30, "+20~30%"),
                        (50, "+30~50%"), (75, "+50~75%"), (1e9, "+75%+")):
            if c < hi:
                return lab
        return None
    print_band("涨幅档 × 多空", band_stats(rows, k_chg, h), h)

    # 2) 市值档
    def k_mc(r):
        mc = r["mc"]
        if mc is None:
            return None
        return "S<300M" if mc < 3e8 else ("M300M~1B" if mc < 1e9 else "L>1B")
    print_band("市值档 × 多空", band_stats(rows, k_mc, h), h)

    # 3) 当日单根 1h 巨阳
    def k_max1h(r):
        mx = r["max1h"]
        if mx is None:
            return None
        m = mx * 100
        for hi, lab in ((10, "max1h<10%"), (20, "10~20%"), (40, "20~40%"), (1e9, "max1h>40%")):
            if m < hi:
                return lab
        return None
    print_band("当日单根 1h 巨阳 × 多空", band_stats(rows, k_max1h, h), h)

    # 4) 放量分档（成交额四分位）
    def k_vol(r):
        v = r["vol"]
        if not v or v <= 0 or not qs:
            return None
        if v < qs[0]:
            return "成交额低"
        if v < qs[1]:
            return "成交额中"
        if v < qs[2]:
            return "成交额高"
        return "成交额最高"
    print_band("成交额分档 × 多空", band_stats(rows, k_vol, h), h)

    # 5) 资金费率
    def k_fund(r):
        rt = r["rate"]
        if rt is None:
            return None
        if rt >= 0.0005:
            return "费率正高"
        if rt >= 0.0001:
            return "费率正"
        if rt > -0.0001:
            return "费率近零"
        return "费率负"
    print_band("资金费率 × 多空", band_stats(rows, k_fund, h), h)

    # 6) 推荐条件组合（基于单因子正期望方向，多因子 AND）
    combos = {
        "做多·温和涨幅+大盘": lambda r: (r["chg"] * 100 < 20) and (r["mc"] and r["mc"] >= 3e8),
        "做多·温和涨幅+大盘+非巨阳": lambda r: (r["chg"] * 100 < 20) and (r["mc"] and r["mc"] >= 3e8)
                                          and (r["max1h"] is not None and r["max1h"] < 0.10),
        "做多·低放量+非巨阳": lambda r: (r["vol"] and qs and r["vol"] < qs[1])
                                    and (r["max1h"] is not None and r["max1h"] < 0.10),
        "做空·极端涨幅": lambda r: r["chg"] * 100 >= 75,
        "做空·极端涨幅+小市值": lambda r: (r["chg"] * 100 >= 75) and (r["mc"] and r["mc"] < 3e8),
        "做空·高涨幅+巨阳": lambda r: (r["chg"] * 100 >= 30) and (r["max1h"] is not None and r["max1h"] >= 0.20),
        "做空·高涨幅+放量": lambda r: (r["chg"] * 100 >= 30) and (r["vol"] and qs and r["vol"] >= qs[2]),
    }
    crows = [(lab, [r for r in rows if fn(r)]) for lab, fn in combos.items()]
    print(f"\n=== 条件组合验证（{h} 日；毛、不计成本） ===")
    print(f"{'组合':>24} {'n':>6} | {'多胜%':>6} {'多期望%':>8} {'多中位%':>7} | "
          f"{'空胜%':>6} {'空期望%':>8} {'空中位%':>7}")
    print("-" * 80)
    for lab, sub in crows:
        if len(sub) < MIN_N:
            print(f"{lab:>24} {len(sub):>6} | 样本不足(<{MIN_N})")
            continue
        stats_rows = [dict(r, **{f"r{h}": r[f"r{h}"]}) for r in sub]
        out = band_stats(stats_rows, lambda _: "_", h)
        n, L, S = next(iter(out.values()))
        print(f"{lab:>24} {n:>6} | {fmt_pct(L[0]):>6} {fmt_pct(L[1]):>8} {fmt_pct(L[2]):>7} | "
              f"{fmt_pct(S[0]):>6} {fmt_pct(S[1]):>8} {fmt_pct(S[2]):>7}")

    # 写 CSV
    out_csv = DATA_DIR / f"backtest_binance_long_short.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "label", "h", "n",
                    "long_win", "long_mean", "long_med", "long_pl",
                    "short_win", "short_mean", "short_med", "short_pl"])
        for title, fn in (("chg", k_chg), ("mcap", k_mc), ("max1h", k_max1h),
                          ("vol", k_vol), ("fund", k_fund)):
            bs = band_stats(rows, fn, h)
            for k, (n, L, S) in bs.items():
                w.writerow([title, k, h, n, *[round(x, 4) if x is not None else "" for x in L],
                            *[round(x, 4) if x is not None else "" for x in S]])
        for lab, sub in crows:
            if len(sub) < MIN_N:
                continue
            out = band_stats([dict(r) for r in sub], lambda _: "_", h)
            n, L, S = next(iter(out.values()))
            w.writerow(["combo", lab, h, n, *[round(x, 4) if x is not None else "" for x in L],
                        *[round(x, 4) if x is not None else "" for x in S]])
    print(f"\n结果已存 {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
