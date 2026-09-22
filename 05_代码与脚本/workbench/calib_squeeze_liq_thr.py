#!/usr/bin/env python3
"""轧空池爆仓阈值标定（复验 P1-b / P2-c：让标定可复现）。

为什么需要这个脚本（复验结论）：
  - 上一版标定只留下结论数字（「7 天 / 8018 行 / 211 币」「n=1202 越阈率 43.2%」），
    脚本未入库 ⇒ 复验按同一文字口径独立实现得到 **n=997 / 51.96% / 17.95%**，
    n 差 17%、越阈率差 8.8pp，**无法定位差异来源**（属过程不可审计）。
  - 且「7 天」前提不成立：`liquidation_snapshot` 标定时全表只覆盖约 6 小时，
    `INTERVAL '7 days'` 形同虚设，行数随时间增长（8018 / 10080 / 25296 都出现过）
    ⇒ 「8018 行」是瞬时值，不是稳定样本。
  - 判据本身也偏弱：单一实现的「点越阈率 < 20%」在另一口径下就能翻盘。

本脚本把三件事写死：
  1. **口径定义**（窗口 / 基准 / 回撤 / 振幅）写进 `variant_*` 函数，可被逐行核对；
  2. **时间边界**：每次运行都打印 min(ts)/max(ts)/实际跨度/行数，避免再拿瞬时值当样本量；
  3. **跨口径上界**：至少跑 3 个变体，取越阈率 max 作为判据输入（P2-c）。

只读：不写任何表。

用法：
    python calib_squeeze_liq_thr.py                  # 默认 7 天窗口 / vol24 ≥ 5e6
    python calib_squeeze_liq_thr.py --days 1 --json  # 缩短窗口 / 输出机器可读
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.analysis import squeeze as sqz  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402

# 标定用的旧值与判据线（写死，便于对照；新值从 squeeze.py 实时读，避免两侧漂移）
OLD_LONG_LIQ_RATIO = 0.00008
JUDGE_UPPER_BOUND_PCT = 20.0   # 判据：条件子集越阈率**跨口径上界** < 20%
WINDOW_MIN = 60                # 条件子集的观察窗（分钟）
SURGE_MIN_PCT = 2.0            # 窗口内拉升幅度下限（%）
RETRACE_MIN_PCT = 2.0          # 距窗口高点回撤幅度下限（%）

SQL_SAMPLE = """
WITH v AS (
    SELECT symbol, SUM(quote_vol) AS vol24
    FROM biz.asset_klines
    WHERE interval = '5m' AND open_time >= NOW() - make_interval(days => %(days)s)
    GROUP BY symbol
    HAVING SUM(quote_vol) >= %(vol_min)s
)
SELECT l.symbol,
       l.ts,
       l.long_liq_usd_1h::float8  AS long_liq,
       l.short_liq_usd_1h::float8 AS short_liq,
       v.vol24::float8            AS vol24,
       w.peak_hi::float8  AS peak_hi,
       w.trough_lo::float8 AS trough_lo,
       w.peak_c::float8   AS peak_c,
       w.trough_c::float8 AS trough_c,
       cn.close_now::float8 AS close_now
FROM biz.liquidation_snapshot l
JOIN v USING (symbol)
JOIN LATERAL (
    SELECT MAX(k.high_px) AS peak_hi, MIN(k.low_px) AS trough_lo,
           MAX(k.close_px) AS peak_c, MIN(k.close_px) AS trough_c
    FROM biz.asset_klines k
    WHERE k.symbol = l.symbol AND k.interval = '5m'
      AND k.open_time > l.ts - make_interval(mins => %(win)s)
      AND k.open_time <= l.ts
) w ON TRUE
JOIN LATERAL (
    SELECT k2.close_px AS close_now
    FROM biz.asset_klines k2
    WHERE k2.symbol = l.symbol AND k2.interval = '5m'
      AND k2.open_time > l.ts - make_interval(mins => %(win)s)
      AND k2.open_time <= l.ts
    ORDER BY k2.open_time DESC LIMIT 1
) cn ON TRUE
WHERE l.ts >= NOW() - make_interval(days => %(days)s)
  AND l.long_liq_usd_1h IS NOT NULL AND l.long_liq_usd_1h > 0
  AND v.vol24 > 0
"""


def pct(vals: list[float], q: float) -> float | None:
    """线性插值分位（等价 `percentile_cont`）。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rate(vals: list[float], thr: float) -> float | None:
    """越阈率（%）。"""
    if not vals:
        return None
    return sum(1 for x in vals if x >= thr) / len(vals) * 100


def variant_a(r) -> bool:
    """A：窗口内拉升（高低点振幅）≥2% 且 已回撤 ≥2%。"""
    if not (r["peak_hi"] and r["trough_lo"] and r["close_now"] and r["trough_lo"] > 0):
        return False
    amp = (r["peak_hi"] - r["trough_lo"]) / r["trough_lo"] * 100
    retrace = (r["peak_hi"] - r["close_now"]) / r["peak_hi"] * 100
    return amp >= SURGE_MIN_PCT and retrace >= RETRACE_MIN_PCT


def variant_b(r) -> bool:
    """B：同 A，但高点/低点改用收盘价（只有 close 时才代表「已实现」涨跌）。"""
    if not (r["peak_c"] and r["trough_c"] and r["close_now"] and r["trough_c"] > 0):
        return False
    amp = (r["peak_c"] - r["trough_c"]) / r["trough_c"] * 100
    retrace = (r["peak_c"] - r["close_now"]) / r["peak_c"] * 100
    return amp >= SURGE_MIN_PCT and retrace >= RETRACE_MIN_PCT


def variant_c(r) -> bool:
    """C：只要求已回撤 ≥2%（不加拉升条件 —— 回撤本身必然蕴含振幅）。"""
    if not (r["peak_hi"] and r["close_now"]):
        return False
    retrace = (r["peak_hi"] - r["close_now"]) / r["peak_hi"] * 100
    return retrace >= RETRACE_MIN_PCT


VARIANTS = {"A 振幅(hi/lo)+回撤": variant_a,
            "B 振幅(close)+回撤": variant_b,
            "C 仅回撤": variant_c}


def main() -> int:
    ap = argparse.ArgumentParser(description="轧空池爆仓阈值标定（只读）")
    ap.add_argument("--days", type=int, default=7, help="样本窗口天数（表里没这么多就是全表）")
    ap.add_argument("--vol24-min", type=float, default=5e6, help="vol24 下限（USDT）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        conn.readonly = True
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT min(ts) AS mn, max(ts) AS mx, count(*) AS n, "
                "       count(DISTINCT symbol) AS syms "
                "FROM biz.liquidation_snapshot")
            bound = cur.fetchone()
            cur.execute(SQL_SAMPLE, {"days": args.days, "vol_min": args.vol24_min,
                                     "win": WINDOW_MIN})
            rows = cur.fetchall()

    span_h = ((bound["mx"] - bound["mn"]).total_seconds() / 3600) if bound["mn"] else 0.0
    ratios = [r["long_liq"] / r["vol24"] for r in rows]
    short_ratios = [r["short_liq"] / r["vol24"] for r in rows
                    if r["short_liq"] is not None]

    p50, p75, p90, p95 = (pct(ratios, q) for q in (0.50, 0.75, 0.90, 0.95))
    new_thr = sqz.LONG_LIQ_RATIO_THR

    subset: dict[str, list[float]] = {}
    for name, fn in VARIANTS.items():
        subset[name] = [r["long_liq"] / r["vol24"] for r in rows if fn(r)]

    out = {
        "table_bound": {
            "min_ts": bound["mn"].isoformat() if bound["mn"] else None,
            "max_ts": bound["mx"].isoformat() if bound["mx"] else None,
            "span_hours": round(span_h, 2),
            "rows": bound["n"], "symbols": bound["syms"],
        },
        "sample": {"rows": len(rows), "symbols": len({r["symbol"] for r in rows}),
                   "window_days_arg": args.days, "vol24_min": args.vol24_min},
        "unconditional": {
            "p50": p50, "p75": p75, "p90": p90, "p95": p95,
            "old_thr": OLD_LONG_LIQ_RATIO, "new_thr": new_thr,
            "rate_old_pct": rate(ratios, OLD_LONG_LIQ_RATIO),
            "rate_new_pct": rate(ratios, new_thr),
        },
        "short_side": {
            "p90": pct(short_ratios, 0.90),
            "rate_min_pct": rate(short_ratios, sqz.SQZ_SHORT_LIQ_RATIO_MIN),
            "threshold": sqz.SQZ_SHORT_LIQ_RATIO_MIN,
        },
        "subsets": {
            name: {"n": len(vals),
                   "rate_p90_pct": rate(vals, p90) if p90 else None,
                   "rate_old_pct": rate(vals, OLD_LONG_LIQ_RATIO),
                   "rate_new_pct": rate(vals, new_thr)}
            for name, vals in subset.items()
        },
    }
    new_rates = [v["rate_new_pct"] for v in out["subsets"].values()
                 if v["rate_new_pct"] is not None]
    out["judge"] = {
        "criterion": f"条件子集越阈率跨口径上界 < {JUDGE_UPPER_BOUND_PCT}%",
        "upper_bound_pct": max(new_rates) if new_rates else None,
        "pass": bool(new_rates) and max(new_rates) < JUDGE_UPPER_BOUND_PCT,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    b = out["table_bound"]
    print("【样本时间边界】← 每次标定必须先看这里（复验 P1-a：别再把瞬时值当「7 天样本」）")
    print(f"  liquidation_snapshot: {b['min_ts']} ~ {b['max_ts']}")
    print(f"  实际跨度 {b['span_hours']} 小时 / 全表 {b['rows']} 行 / {b['symbols']} 币")
    print(f"  本次入样本：{out['sample']['rows']} 行 / {out['sample']['symbols']} 币"
          f"（窗口参数 {args.days} 天，vol24 ≥ {args.vol24_min:g}）")
    print("\n【无条件分布】long_liq / vol24")
    print(f"  P50={p50:.3e} P75={p75:.3e} P90={p90:.3e} P95={p95:.3e}")
    print(f"  旧值 {OLD_LONG_LIQ_RATIO:.0e} 越阈率 {out['unconditional']['rate_old_pct']:.2f}%"
          f"  |  新值 {new_thr:.8f} 越阈率 "
          f"{out['unconditional']['rate_new_pct']:.2f}%")
    s = out["short_side"]
    print(f"\n【入队侧对照】short_liq / vol24：P90={s['p90']:.3e}，"
          f"阈值 {s['threshold']:.0e} 越阈率 {s['rate_min_pct']:.2f}%"
          f"（设计目标 ≈10%，即落在 P90；显著偏离说明样本期间市场状态不同）")
    print(f"\n【条件子集（窗口 {WINDOW_MIN}min，拉升 ≥{SURGE_MIN_PCT}% / 回撤 ≥{RETRACE_MIN_PCT}%）】")
    for name, v in out["subsets"].items():
        r_new = f"{v['rate_new_pct']:.2f}%" if v["rate_new_pct"] is not None else "n/a"
        r_old = f"{v['rate_old_pct']:.2f}%" if v["rate_old_pct"] is not None else "n/a"
        print(f"  {name:<18} n={v['n']:<6} 旧值越阈 {r_old:>8}  新值越阈 {r_new:>8}")
    j = out["judge"]
    ub = f"{j['upper_bound_pct']:.2f}%" if j["upper_bound_pct"] is not None else "n/a"
    print(f"\n【判定】{j['criterion']}")
    print(f"  跨口径上界 = {ub}  →  {'PASS' if j['pass'] else 'FAIL'}"
          f"（{'' if j['pass'] else '需回退到 P90 或放宽判据'}）")
    print("\n⚠️ 样本时间代表性弱（表历史见上）⇒ 结论仅供临时定稿，"
          "待 squeeze_track 判定样本积累后改用判定窗口直接标定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())