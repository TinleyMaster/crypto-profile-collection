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

本脚本把五件事写死：
  1. **口径定义**（窗口 / 基准 / 回撤 / 振幅）写进 `variant_*` 函数，可被逐行核对；
  2. **时间边界**：每次运行都打印 min(ts)/max(ts)/实际跨度/行数，避免再拿瞬时值当样本量；
  3. **跨口径上界**：至少跑 3 个变体，取越阈率 max 作为判据输入（P2-c）；
  4. **分母自证**（复验 P1-1a / D2）：打印每币在分母窗口内的 5m 根数 / 期望
     （`bars / (days×288)`）与实际覆盖小时数；**整体覆盖 < 0.9 或「低于门槛的币
     占比 > 5%」时拒绝出结论**（exit 3）——只看均值会被少数劣质币蒙混过关。
     原因：阈值语义是「爆仓额 / 24h 成交额」，若 `asset_klines`
     在该窗口缺小时，分母被系统性少算 ⇒ 同一份爆仓数据算出的越阈率被放大
     **1.66×**（复验 §2；绝对数字随表滚动，一律以本次实跑为准）。
  5. **long 侧双口径**（复验 D3）：`long_liq > 0` 子集（判据所用）与**含 0** 口径
     并列输出——`long_liq = 0` 是「该 1h 无多头爆仓」的合法观测，排除它会系统性
     抬高触发频率（实测 1.34×）。

口径澄清（复验 P1-1b/c）：
  - 窗口参数默认 **1 天**——与「/24h 成交额」语义一致；旧默认 7 天会得 3× 偏差
    （实测同一数据 24.76% ↔ 8.32%）；
  - `asset_klines` 的成交额别名改叫 `vol_win`（不再叫 `vol24`，避免被误当严格 24h）；
  - 窗口高低点/收盘取 `open_time < l.ts`——旧写 `<= l.ts` 会取到**覆盖
    `[l.ts, l.ts+5m)` 的桶**，即判定时刻**之后** 5 分钟的量价（未来函数）。

只读：不写任何表。

用法：
    python calib_squeeze_liq_thr.py                  # 默认 1 天窗口 / vol_win ≥ 5e6
    python calib_squeeze_liq_thr.py --days 7 --json  # 拉长窗口 / 输出机器可读
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
BUCKETS_PER_DAY = 288          # 5m 桶 / 天（分母窗口完成度的期望值基准）
MIN_DENOM_COVERAGE = 0.9       # 分母窗口整体覆盖下限，低于此值拒绝出结论（exit 3）
# 复验 D2：只看「整体均值」会被少数劣质币蒙混过关（算术例：249 币里 27 币仅 10% 覆盖
# ⇒ 均值仍 0.902 过 0.9 线，而这 27 币的 vol_win 被少算 10×、越阈率被放大 10× 且照样入样污染分布）
# ⇒ 同时卡「低于门槛的币占比」。
MAX_DENOM_BELOW_PCT = 5.0      # 低于 MIN_DENOM_COVERAGE 的币占比上限（%），超过即拒绝出结论

SQL_SAMPLE = """
WITH v AS (
    SELECT symbol, SUM(quote_vol) AS vol_win
    FROM biz.asset_klines
    WHERE interval = '5m' AND open_time >= NOW() - make_interval(days => %(days)s)
    GROUP BY symbol
    HAVING SUM(quote_vol) >= %(vol_min)s
)
SELECT l.symbol,
       l.ts,
       l.long_liq_usd_1h::float8  AS long_liq,
       l.short_liq_usd_1h::float8 AS short_liq,
       v.vol_win::float8          AS vol_win,
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
      AND k.open_time < l.ts
) w ON TRUE
JOIN LATERAL (
    SELECT k2.close_px AS close_now
    FROM biz.asset_klines k2
    WHERE k2.symbol = l.symbol AND k2.interval = '5m'
      AND k2.open_time > l.ts - make_interval(mins => %(win)s)
      AND k2.open_time < l.ts
    ORDER BY k2.open_time DESC LIMIT 1
) cn ON TRUE
WHERE l.ts >= NOW() - make_interval(days => %(days)s)
  AND l.long_liq_usd_1h IS NOT NULL AND l.long_liq_usd_1h > 0
  AND v.vol_win > 0
"""

# 复验 D3：long 侧「含 0」口径（去掉上方 `long_liq > 0` 过滤）。
# `long_liq = 0` 是「该 1h 无多头爆仓」的**合法观测**，且永远不可能越阈 ⇒ 只报过滤后子集
# 等于系统性抬高触发频率（实测两口径差 **1.34×**，绝对数字随表滚动，以实跑为准）。
# 这里只需要无条件分布，不取窗口高低点 ⇒ 省掉两个 LATERAL，开销小。
SQL_LONG_INCL0 = """
WITH v AS (
    SELECT symbol, SUM(quote_vol) AS vol_win
    FROM biz.asset_klines
    WHERE interval = '5m' AND open_time >= NOW() - make_interval(days => %(days)s)
    GROUP BY symbol
    HAVING SUM(quote_vol) >= %(vol_min)s
)
SELECT l.long_liq_usd_1h::float8 / v.vol_win::float8 AS ratio
FROM biz.liquidation_snapshot l
JOIN v USING (symbol)
WHERE l.ts >= NOW() - make_interval(days => %(days)s)
  AND l.long_liq_usd_1h IS NOT NULL
  AND v.vol_win > 0
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


def _f(v, spec: str = ".3e", dash: str = "n/a") -> str:
    """None 安全的数值格式化（分位在空样本上是 None，不能直接 f-string）。"""
    return dash if v is None else format(v, spec)


def denominator_coverage(cur, syms: list[str], days: int) -> dict:
    """分母窗口自证（复验 P1-1a）：每币 5m 根数 / 期望 + 实际覆盖小时数。

    `vol_win` = `SUM(quote_vol)` over `days` 天，其可用性完全取决于 `asset_klines`
    在该窗口内的完成度：表缺小时 ⇒ 分母被少算 ⇒ 越阈率整体被放大
    （实测同一份爆仓数据：完整分母 24.58% ↔ 缺 14h 分母 40.21%，差 1.66×）。
    只评估「本次入样币集合」——被少算的正是这些币，越阈率被放大的也是它们。
    """
    expect_bars = max(1, days * BUCKETS_PER_DAY)
    expect_hours = max(1, days * 24)
    out = {"days": days, "symbols": len(syms), "expect_bars_per_symbol": expect_bars,
           "expect_hours": expect_hours, "bars_min": None, "bars_median": None,
           "bars_max": None, "hours_median": None, "coverage": None,
           "bars_ratio_p10": None, "symbols_below": 0, "symbols_below_pct": 0.0,
           "worst": [], "per_symbol": []}
    if not syms:
        return out
    cur.execute(
        "SELECT symbol, count(*) AS bars, "
        "       count(DISTINCT date_trunc('hour', open_time)) AS hours "
        "FROM biz.asset_klines "
        "WHERE interval = '5m' "
        "  AND open_time >= NOW() - make_interval(days => %(days)s) "
        "  AND symbol = ANY(%(syms)s) "
        "GROUP BY symbol", {"days": days, "syms": syms})
    cnt = {r["symbol"]: (int(r["bars"]), int(r["hours"])) for r in cur.fetchall()}
    per = [{"symbol": s, "bars": cnt.get(s, (0, 0))[0], "hours": cnt.get(s, (0, 0))[1],
            "bars_ratio": round(cnt.get(s, (0, 0))[0] / expect_bars, 3)} for s in syms]
    bars = sorted(d["bars"] for d in per)
    hours = sorted(d["hours"] for d in per)
    out["per_symbol"] = per
    out["bars_min"], out["bars_max"] = bars[0], bars[-1]
    out["bars_median"] = bars[len(bars) // 2]
    out["hours_median"] = hours[len(hours) // 2]
    out["coverage"] = sum(bars) / (len(syms) * expect_bars)
    out["symbols_below"] = sum(1 for b in bars if b < expect_bars * MIN_DENOM_COVERAGE)
    out["symbols_below_pct"] = round(out["symbols_below"] / len(syms) * 100, 2)
    out["bars_ratio_p10"] = pct([b / expect_bars for b in bars], 0.10)
    out["worst"] = sorted(per, key=lambda d: d["bars_ratio"])[:5]
    return out


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
    ap.add_argument("--days", type=int, default=1,
                    help="样本窗口天数（默认 1，与「/24h 成交额」语义一致；旧默认 7 会得 3× 偏差）")
    ap.add_argument("--vol-win-min", "--vol24-min", dest="vol_win_min", type=float,
                    default=5e6, help="vol_win 下限（USDT，别名 --vol24-min）")
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
            cur.execute(SQL_SAMPLE, {"days": args.days, "vol_min": args.vol_win_min,
                                     "win": WINDOW_MIN})
            rows = cur.fetchall()
            # 分母自证（复验 P1-1a）：入样币集合上的 5m 根数 / 覆盖小时
            denom = denominator_coverage(cur, sorted({r["symbol"] for r in rows}), args.days)
            # long 侧「含 0」口径（复验 D3）
            cur.execute(SQL_LONG_INCL0, {"days": args.days, "vol_min": args.vol_win_min})
            ratios_incl0 = [r["ratio"] for r in cur.fetchall()]

    span_h = ((bound["mx"] - bound["mn"]).total_seconds() / 3600) if bound["mn"] else 0.0
    ratios = [r["long_liq"] / r["vol_win"] for r in rows]
    short_all = [r["short_liq"] / r["vol_win"] for r in rows
                 if r["short_liq"] is not None]
    short_gt0 = [r["short_liq"] / r["vol_win"] for r in rows
                 if r["short_liq"] is not None and r["short_liq"] > 0]

    p50, p75, p90, p95 = (pct(ratios, q) for q in (0.50, 0.75, 0.90, 0.95))
    new_thr = sqz.LONG_LIQ_RATIO_THR

    subset: dict[str, list[float]] = {}
    for name, fn in VARIANTS.items():
        subset[name] = [r["long_liq"] / r["vol_win"] for r in rows if fn(r)]

    cov = denom["coverage"] or 0.0
    # 复验 D2：闸门不能只看整体均值——少数币被少算时均值照样能过线
    below_pct = denom["symbols_below_pct"] or 0.0
    denom_fail: list[str] = []
    if cov < MIN_DENOM_COVERAGE:
        denom_fail.append(f"整体覆盖 {cov:.3f} < {MIN_DENOM_COVERAGE}")
    if below_pct > MAX_DENOM_BELOW_PCT:
        denom_fail.append(f"低于门槛的币 {denom['symbols_below']}/{denom['symbols']}"
                          f" = {below_pct:.1f}% > {MAX_DENOM_BELOW_PCT}%")
    denom_ok = not denom_fail
    out = {
        "table_bound": {
            "min_ts": bound["mn"].isoformat() if bound["mn"] else None,
            "max_ts": bound["mx"].isoformat() if bound["mx"] else None,
            "span_hours": round(span_h, 2),
            "rows": bound["n"], "symbols": bound["syms"],
        },
        "denominator": denom,
        "denominator_ok": denom_ok,
        "sample": {"rows": len(rows), "symbols": len({r["symbol"] for r in rows}),
                   "window_days_arg": args.days, "vol_win_min": args.vol_win_min},
        "unconditional": {
            "p50": p50, "p75": p75, "p90": p90, "p95": p95,
            "old_thr": OLD_LONG_LIQ_RATIO, "new_thr": new_thr,
            "rate_old_pct": rate(ratios, OLD_LONG_LIQ_RATIO),
            "rate_new_pct": rate(ratios, new_thr),
        },
        # 复验 D3：long 侧「含 0」口径（long_liq=0 是合法观测，永不可能越阈；
        # 只报过滤后子集会系统性抬高触发频率）
        "unconditional_incl0": {
            "n": len(ratios_incl0),
            "p50": pct(ratios_incl0, 0.50), "p75": pct(ratios_incl0, 0.75),
            "p90": pct(ratios_incl0, 0.90), "p95": pct(ratios_incl0, 0.95),
            "rate_old_pct": rate(ratios_incl0, OLD_LONG_LIQ_RATIO),
            "rate_new_pct": rate(ratios_incl0, new_thr),
        },
        "short_side": {
            "p90_gt0": pct(short_gt0, 0.90),
            "n_incl0": len(short_all), "n_gt0": len(short_gt0),
            "rate_incl0_pct": rate(short_all, sqz.SQZ_SHORT_LIQ_RATIO_MIN),
            "rate_gt0_pct": rate(short_gt0, sqz.SQZ_SHORT_LIQ_RATIO_MIN),
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
        # 复验 D1：pass 必须与 denominator_ok 同向——分母不合格时算出的上界是纯噪音，
        # 否则 `--json` 会输出「pass=true」而进程 rc=3，下游读 JSON 必然误判。
        "pass": bool(denom_ok and new_rates) and max(new_rates) < JUDGE_UPPER_BOUND_PCT,
        "reliable": denom_ok,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if denom_ok else 3

    b = out["table_bound"]
    print("【样本时间边界】← 每次标定必须先看这里（复验 P1-a：别再把瞬时值当「7 天样本」）")
    print(f"  liquidation_snapshot: {b['min_ts']} ~ {b['max_ts']}")
    print(f"  实际跨度 {b['span_hours']} 小时 / 全表 {b['rows']} 行 / {b['symbols']} 币")
    print(f"  本次入样本：{out['sample']['rows']} 行 / {out['sample']['symbols']} 币"
          f"（窗口参数 {args.days} 天，vol_win ≥ {args.vol_win_min:g}）")

    d = denom
    print("\n【分母自证】← 复验 P1-1a：分母窗口不完整会把越阈率整体放大（实测 1.66×）")
    print(f"  asset_klines(5m) 窗口 {d['days']} 天：期望每币 {d['expect_bars_per_symbol']} 根"
          f" = 288×{d['days']}（{d['expect_hours']} 小时）")
    print(f"  实测每币根数 min={_f(d['bars_min'], 'd')} 中位={_f(d['bars_median'], 'd')}"
          f" max={_f(d['bars_max'], 'd')}"
          f"  | 覆盖小时中位 {_f(d['hours_median'], 'd')}/{d['expect_hours']}")
    print(f"  整体覆盖 = {cov:.3f}（Σ根数 / 期望总数，门槛 {MIN_DENOM_COVERAGE}）"
          f"；低于门槛的币 {d['symbols_below']}/{d['symbols']} = {below_pct:.1f}%"
          f"（上限 {MAX_DENOM_BELOW_PCT}%）")
    print(f"  每币覆盖率 P10 = {_f(d['bars_ratio_p10'], '.3f')}"
          f"（均值会被少数劣质币蒙混，故另卡低于门槛的币占比）")
    if d["worst"]:
        print("  最差 5 币：" + " ".join(
            f"{w['symbol']}={w['bars']}({w['bars_ratio']:.2f}×288×{d['days']})"
            for w in d["worst"]))
    if not denom_ok:
        print("\n🛑 拒绝出结论：" + "；".join(denom_fail)
              + " ⇒ `vol_win` 被系统性少算，任何越阈率都不可比。")
        print("   先补齐 biz.asset_klines(5m)，或改用落在有数据时段的 --days，再重跑。")
        return 3

    print("\n【无条件分布】long_liq / vol_win")
    print("  ——主口径：仅 long_liq > 0（下方全部输出与判据均基于此子集）——")
    print(f"  P50={p50:.3e} P75={p75:.3e} P90={p90:.3e} P95={p95:.3e}")
    print(f"  旧值 {OLD_LONG_LIQ_RATIO:.0e} 越阈率 {out['unconditional']['rate_old_pct']:.2f}%"
          f"  |  新值 {new_thr:.8f} 越阈率 "
          f"{out['unconditional']['rate_new_pct']:.2f}%")
    z = out["unconditional_incl0"]
    print("  ——含 0 口径（复验 D3：long_liq=0 是合法观测，永不可能越阈）——")
    print(f"  n={z['n']:<7} P50={_f(z['p50'])} P75={_f(z['p75'])}"
          f" P90={_f(z['p90'])} P95={_f(z['p95'])}")
    print(f"  旧值越阈率 {_f(z['rate_old_pct'], '.2f')}%"
          f"  |  新值越阈率 {_f(z['rate_new_pct'], '.2f')}%"
          f"  ← 与主口径之差即「排除零爆仓样本」带来的触发频率虚高")
    s = out["short_side"]
    print(f"\n【入队侧对照】short_liq / vol_win：阈值 {s['threshold']:.0e}"
          f"（位置随数据集变化，以本次实跑双率为准；勿引用历史分位）")
    print(f"  含 0 ：n={s['n_incl0']:<6} 越阈率 {_f(s['rate_incl0_pct'], '.2f')}%")
    print(f"  >0   ：n={s['n_gt0']:<6} 越阈率 {_f(s['rate_gt0_pct'], '.2f')}%"
          f"（P90={_f(s['p90_gt0'])}）")
    print(f"\n【条件子集（窗口 {WINDOW_MIN}min，拉升 ≥{SURGE_MIN_PCT}% / 回撤 ≥{RETRACE_MIN_PCT}%）】")
    for name, v in out["subsets"].items():
        r_new = f"{v['rate_new_pct']:.2f}%" if v["rate_new_pct"] is not None else "n/a"
        r_old = f"{v['rate_old_pct']:.2f}%" if v["rate_old_pct"] is not None else "n/a"
        print(f"  {name:<18} n={v['n']:<6} 旧值越阈 {r_old:>8}  新值越阈 {r_new:>8}")
    j = out["judge"]
    ub = f"{j['upper_bound_pct']:.2f}%" if j["upper_bound_pct"] is not None else "n/a"
    print(f"\n【判定】{j['criterion']}")
    print(f"  跨口径上界 = {ub}  →  {'PASS' if j['pass'] else 'FAIL'}"
          f"（{'' if j['pass'] else '先核对分母覆盖与未来函数，再谈调阈值'}）")
    print("\n⚠️ 样本时间代表性弱（表历史见上）⇒ 结论仅供临时定稿，"
          "待 squeeze_track 判定样本积累后改用判定窗口直接标定。")
    return 0 if j["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())