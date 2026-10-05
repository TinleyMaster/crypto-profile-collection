#!/usr/bin/env python3
"""涨幅榜短线做多结论 · 加固验证（2023~2026 周期，529 合约 1h）。

针对「短线（T+24h）做多高涨幅 +15~25%」这一结论的三个系统性偏差做量化复核：
  1. 扣 BTC beta（β=1 简单超额 net24 = r24h - btc_r24h）→ 看还剩多少净 alpha
  2. 独立事件去重（每币每 72h 只保留第一个事件）→ 消除同行情重复计数
  3. 成本敏感性（往返成本 0.1% / 0.3%）→ 看哪些信号被成本吃掉

对照原始口径重算：C 表（涨异动 × 当日涨幅分档 → 做多）与 E 表（≥30% × 异动）。

用法：
    python backtest_robust_checks.py
"""
from __future__ import annotations

import csv
import statistics
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

MIN_N = 30
COSTS = (0.001, 0.003)   # 往返成本 0.1% / 0.3%
DATA_DIR = SCRIPT_DIR.parent / "data"
EVENTS_CACHE = DATA_DIR / "robust_events_cache.csv"   # SQL 中间结果缓存（重跑免 25min SQL）
FLOAT_KEYS = ("chg1h", "vr", "r24h", "chg24", "btc_r24")

# 同 combined：LEAD 一次算 T+24h；chg24 用 LAG 滚动 24h 涨幅（替代 daily CTE，快且更贴合
# 涨幅榜滚动口径）；另加 BTC 同步 24h 收益（β=1 超额基准）。
SQL = """
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
)
SELECT e.symbol, e.open_time AS eo, e.chg1h, e.vr, e.r24h, e.chg24,
       b.px24 / b.close_px - 1 AS btc_r24
FROM evt e
LEFT JOIN btc b ON b.open_time = e.open_time
"""


def agg(xs):
    n = len(xs)
    return (n, sum(1 for x in xs if x > 0) / n, statistics.fmean(xs),
            statistics.median(xs))


def fmt(x):
    return "—" if x is None else f"{x*100:.2f}"


def report(title, sub, net_key, min_n=MIN_N):
    """输出原始 / 扣beta / 独立事件 / 成本后的做多期望（T+24h）。"""
    print(f"\n== {title} ==")
    print(f"{'口径':>12} {'n':>7} {'胜%':>6} {'期望%':>8} {'中位%':>7}")
    print("-" * 46)
    if len(sub) < min_n:
        print("样本不足")
        return
    # 原始
    n, w, m, md = agg([r["r24h"] for r in sub])
    print(f"{'原始':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")
    # 扣 beta（β=1 超额）
    subn = [r for r in sub if r["btc_r24"] is not None]
    if len(subn) >= min_n:
        n, w, m, md = agg([r["r24h"] - r["btc_r24"] for r in subn])
        print(f"{'扣BTCβ(β=1)':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")
    # 独立事件（每币 72h 去重）
    subi = dedup72(sub)
    if len(subi) >= min_n:
        n, w, m, md = agg([r["r24h"] for r in subi])
        print(f"{'独立事件72h':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")
        # 独立事件 + 扣 beta
        subin = [r for r in subi if r["btc_r24"] is not None]
        if len(subin) >= min_n:
            n, w, m, md = agg([r["r24h"] - r["btc_r24"] for r in subin])
            print(f"{'独立+扣β':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")
    # 成本敏感性（原始，往返成本）
    for c in COSTS:
        n, w, m, md = agg([r["r24h"] - c for r in sub])
        print(f"{f'扣成本{c*100:.1f}%':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")


def dedup72(rows):
    """每币每 72h 保留第一个事件（按时间排序，前一个保留事件 +72h 内的丢弃）。"""
    bysym: dict[str, list] = {}
    for r in rows:
        bysym.setdefault(r["symbol"], []).append(r)
    out = []
    for sym, rs in bysym.items():
        rs.sort(key=lambda r: r["eo"])
        last = None
        for r in rs:
            if last is None or (r["eo"] - last).total_seconds() >= 72 * 3600:
                out.append(r)
                last = r["eo"]
    return out


def load_events() -> list[dict]:
    """取数：优先读本地缓存，否则用 COPY 流式把 SQL 结果落盘缓存（比 fetchall 快且省内存）。"""
    if EVENTS_CACHE.exists():
        with EVENTS_CACHE.open(newline="") as f:
            rd = csv.DictReader(f)
            rows = [dict(r) for r in rd]
        for r in rows:
            r["eo"] = datetime.fromisoformat(r["eo"])
            for k in FLOAT_KEYS:
                v = r.get(k)
                r[k] = None if v in (None, "") else float(v)
        return rows

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            with cur.copy(f"COPY ({SQL}) TO STDOUT WITH (FORMAT csv, HEADER true)") as cp:
                with EVENTS_CACHE.open("wb") as f:
                    while (chunk := cp.read()) is not None:
                        f.write(chunk)
    # psycopg 默认把 numeric 映射成 Decimal，统一转 float 便于后续运算
    with EVENTS_CACHE.open(newline="") as f:
        rd = csv.DictReader(f)
        rows = [dict(r) for r in rd]
    for r in rows:
        r["eo"] = datetime.fromisoformat(r["eo"])
        for k in FLOAT_KEYS:
            v = r.get(k)
            r[k] = None if v in (None, "") else float(v)
    return rows


def main() -> int:
    rows = load_events()
    print(f"[events] {len(rows):,} 条；独立事件(72h去重) {len(dedup72(rows)):,} 条")

    up = [r for r in rows if r["chg1h"] >= 0.03 and r["vr"] >= 2 and r["chg24"] is not None]

    # C 表：涨异动 × 当日涨幅分档 → 做多 T+24h（加固复核）
    for lo, hi, lab in ((0, 5, "C·当日<5%(未上榜)"),
                        (5, 20, "C·当日+5~20%"),
                        (20, 50, "C·当日+20~50%"),
                        (50, 1e9, "C·当日+50%+"),
                        (30, 1e9, "C·当日+30%+")):
        sub = [r for r in up if (r["chg24"] * 100) >= lo and (r["chg24"] * 100) < hi]
        report(lab, sub, "r24h")

    # E 表：涨幅≥30% × 有无异动 → 做多
    hi = [r for r in rows if r["chg24"] is not None and r["chg24"] * 100 >= 30]
    for lab, cond in (("E·≥30% 有涨异动", lambda r: r["chg1h"] >= 0.03 and r["vr"] >= 2),
                      ("E·≥30% 无异动", lambda r: abs(r["chg1h"]) < 0.03 or r["vr"] < 2)):
        sub = [r for r in hi if cond(r)]
        report(lab, sub, "r24h")

    # 诱多做空（未上榜放量大阳 → 做空 T+24h）+ 成本敏感性
    trap = [r for r in up if (r["chg24"] * 100) < 5]
    print(f"\n== 诱多做空（未上榜放量大阳 → 做空 T+24h）==")
    print(f"{'口径':>12} {'n':>7} {'胜%':>6} {'期望%':>8} {'中位%':>7}")
    print("-" * 46)
    if len(trap) >= MIN_N:
        n, w, m, md = agg([-r["r24h"] for r in trap])
        print(f"{'原始做空':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")
        for c in COSTS:
            n, w, m, md = agg([-r["r24h"] - c for r in trap])
            print(f"{f'扣成本{c*100:.1f}%':>12} {n:>7} {w*100:>6.1f} {fmt(m):>8} {fmt(md):>7}")

    # 7 日做空（中线结论，原 long_short）：这里用 T+24h 事件口径做成本敏感性参考
    # （真正的 7 日做空结论来自 backtest_binance_long_short，此处聚焦短线复核）
    out_csv = DATA_DIR / "backtest_robust_checks.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "label", "method", "n", "win", "mean", "med"])
        sections = [
            ("C_trap", "当日<5%", trap),
            ("C", "+20~50%", [r for r in up if 20 <= r["chg24"] * 100 < 50]),
            ("C", "+50%+", [r for r in up if r["chg24"] * 100 >= 50]),
            ("C", "+30%+", [r for r in up if r["chg24"] * 100 >= 30]),
            ("E", "≥30%有涨异动", [r for r in hi if r["chg1h"] >= 0.03 and r["vr"] >= 2]),
            ("E", "≥30%无异动", [r for r in hi if abs(r["chg1h"]) < 0.03 or r["vr"] < 2]),
        ]
        for sec, lab, sub in sections:
            if len(sub) < MIN_N:
                continue
            n, w, m, md = agg([r["r24h"] for r in sub])
            w.writerow([sec, lab, "raw", n, round(w, 4), round(m, 4), round(md, 4)])
            subn = [r for r in sub if r["btc_r24"] is not None]
            if len(subn) >= MIN_N:
                n, w, m, md = agg([r["r24h"] - r["btc_r24"] for r in subn])
                w.writerow([sec, lab, "net_btc", n, round(w, 4), round(m, 4), round(md, 4)])
            subi = dedup72(sub)
            if len(subi) >= MIN_N:
                n, w, m, md = agg([r["r24h"] for r in subi])
                w.writerow([sec, lab, "indep72", n, round(w, 4), round(m, 4), round(md, 4)])
            for c in COSTS:
                n, w, m, md = agg([r["r24h"] - c for r in sub])
                w.writerow([sec, lab, f"cost{c*100:.1f}%", n, round(w, 4), round(m, 4), round(md, 4)])
    print(f"\n结果已存 {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
