#!/usr/bin/env python3
"""主池 L1 触发根选择单测（P1「后半段才完成的异动」漏检修法）。

运行：python test_scan_l1_closed_bar.py

背景（AGENTS.md ⑥）：`scan_klines` 每 5min 把**未收盘**的 15m/1h 条 UPSERT 覆盖，
同一行 `close_px`/`quote_vol` 随时间内变；旧 `_l1_screen` 只取 `closes[-1]` ⇒ 一根条
「终值可用且仍是最新根」的窗口仅约 2~5 分钟，而扫描每 15min 一轮 ⇒ 能否看到收盘
终值取决于相位（离线量化：真异动条 1719 根中 53.5% 只有收盘才越阈）。

修法：每周期的触发根**优先取最近一根已收盘条**（值稳定 ⇒ 判定可复现），不合格再
回退未收盘条（保持及时性）；入场/失效位锚定被判定的那一根（`l1["bar_idx"]`）。

覆盖：
  1) 仅已收盘条越阈 → 命中且 `bar_idx` 指向该根（旧实现必漏，用例 1b 做回归护栏）；
  2) 仅未收盘条越阈 → 回退分支命中（及时性不丢）；
  3) 两根都越阈 → 取已收盘那根（可复现）；
  4) 最后一根已收盘（自然情形）不重复评估；
  5) 新鲜度护栏（陈旧周期跳过 / 根数不足跳过）；
  6) 多周期取 level 最高者（旧行为不回归）+ 5m 仍为 level 1；
  7) 量比不足不命中；
  8) `_atr_stop_pct` 锚定判定根：截断切片 vs 全量切片结果不同。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_daemon as sd  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name}")
        if detail:
            print(f"    {detail}")


def bar(t, close, vol=100.0, high=None, low=None):
    return {
        "open_time": t,
        "close_px": close,
        "quote_vol": vol,
        "high_px": high if high is not None else close * 1.002,
        "low_px": low if low is not None else close * 0.998,
    }


def series(iv, n, now, unclosed=True, closed_move_pct=0.0, last_move_pct=0.0,
           closed_vol=300.0, last_vol=300.0):
    """构造 n 根 iv 周期 K 线（基准价 100 / 基准量 100）。

    `closed_move_pct`：倒数第二根（= 最近一根已收盘条）相对其前一根的涨跌幅；
    `last_move_pct`：最后一根相对倒数第二根的涨跌幅。
    """
    dur = sd.INTERVAL_SECONDS[iv]
    last_open = now - timedelta(seconds=(dur / 3 if unclosed else dur + 60))
    rows = [bar(last_open - timedelta(seconds=dur * (n - 1 - i)), 100.0) for i in range(n)]
    if closed_move_pct:
        rows[-2] = bar(rows[-2]["open_time"], 100.0 * (1 + closed_move_pct / 100),
                       vol=closed_vol)
    if last_move_pct:
        base = rows[-2]["close_px"]
        rows[-1] = bar(rows[-1]["open_time"], base * (1 + last_move_pct / 100),
                       vol=last_vol)
    return rows


def old_l1_screen(klines_by_iv, now):
    """改造前的实现（只取 closes[-1] / vols[-1]），仅用于回归护栏对照。"""
    best, best_level = None, 0
    for iv in ("1h", "15m", "5m"):
        rows = klines_by_iv.get(iv, [])
        if len(rows) < sd.LOOKBACK_BARS_MAIN + 1:
            continue
        if (now - rows[-1]["open_time"]).total_seconds() / 60 > sd.MAX_KLINE_AGE_MIN[iv]:
            continue
        closes = [float(r["close_px"]) for r in rows]
        vols = [float(r["quote_vol"]) for r in rows]
        chg = (closes[-1] - closes[-2]) / closes[-2] * 100
        vol_mean = sum(vols[-(sd.LOOKBACK_BARS_MAIN + 1):-1]) / sd.LOOKBACK_BARS_MAIN
        vol_ratio = vols[-1] / vol_mean if vol_mean else 0.0
        if abs(chg) >= sd.PRICE_THR[iv] and vol_ratio >= sd.VOL_RATIO_THR:
            level = sd.LEVEL_RANK[iv]
            if level > best_level:
                best_level = level
                best = {"iv": iv, "dir": "up" if chg > 0 else "down",
                        "chg_pct": chg, "vol_ratio": vol_ratio, "level": level}
    return best


NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

print("\n【测试1】仅「已收盘条」越阈（旧实现必漏 → 新实现命中）")
rows15 = series("15m", 25, NOW, unclosed=True, closed_move_pct=3.0, last_move_pct=0.2)
r = sd._l1_screen({"15m": rows15}, NOW)
check(r is not None and r["level"] == 2 and r["iv"] == "15m", "命中且 level=2 / iv=15m", f"got={r}")
check(r is not None and r["bar_idx"] == len(rows15) - 2, "bar_idx 指向已收盘那根", f"got={r}")
check(r is not None and r["dir"] == "up" and abs(r["chg_pct"] - 3.0) < 0.3,
      "方向 up + chg≈3.0", f"got={r}")
old = old_l1_screen({"15m": rows15}, NOW)
check(old is None or old["level"] < 2, "对照：旧实现同样数据不命中（回归护栏）", f"old={old}")

print("\n【测试2】仅「未收盘条」越阈 → 回退分支命中（及时性不丢）")
rows15b = series("15m", 25, NOW, unclosed=True, closed_move_pct=0.1, last_move_pct=3.0,
                 closed_vol=100.0)
r2 = sd._l1_screen({"15m": rows15b}, NOW)
check(r2 is not None and r2["level"] == 2, "回退命中且 level=2", f"got={r2}")
check(r2 is not None and r2["bar_idx"] == len(rows15b) - 1, "bar_idx 指向未收盘那根", f"got={r2}")

print("\n【测试3】两根都越阈 → 取已收盘那根（值稳定、可复现）")
rows15c = series("15m", 25, NOW, unclosed=True, closed_move_pct=3.0, last_move_pct=6.0)
r3 = sd._l1_screen({"15m": rows15c}, NOW)
check(r3 is not None and r3["bar_idx"] == len(rows15c) - 2, "bar_idx = 已收盘根", f"got={r3}")
check(r3 is not None and abs(r3["chg_pct"] - 3.0) < 0.3, "chg 取已收盘根（≈3.0 而非 ≈6.0）",
      f"got={r3}")

print("\n【测试4】最后一根已收盘（自然情形）→ 直接判它，不重复评估")
rows15d = series("15m", 25, NOW, unclosed=False, closed_move_pct=0.1, last_move_pct=3.0,
                 closed_vol=100.0)
r4 = sd._l1_screen({"15m": rows15d}, NOW)
check(r4 is not None and r4["bar_idx"] == len(rows15d) - 1, "命中且 bar_idx = 最后一根", f"got={r4}")

print("\n【测试5】新鲜度与样本量护栏")
stale = [dict(x) for x in series("15m", 25, NOW, closed_move_pct=3.0, last_move_pct=0.2)]
for x in stale:
    x["open_time"] = x["open_time"] - timedelta(minutes=sd.MAX_KLINE_AGE_MIN["15m"] + 5)
check(sd._l1_screen({"15m": stale}, NOW) is None, "15m 陈旧（>35min）→ 该周期跳过")
check(sd._l1_screen({"15m": series("15m", 10, NOW, closed_move_pct=9.0)}, NOW) is None,
      "根数不足 LOOKBACK+1 → 跳过")

print("\n【测试6】多周期取 level 最高者 + 5m 仍为 level 1（旧行为不回归）")
r6 = sd._l1_screen({"5m": series("5m", 25, NOW, closed_move_pct=2.0, last_move_pct=0.2)}, NOW)
check(r6 is not None and r6["level"] == 1 and r6["iv"] == "5m", "5m 达标 → level=1", f"got={r6}")
r7 = sd._l1_screen({"1h": series("1h", 25, NOW, closed_move_pct=4.0, last_move_pct=0.2),
                    "15m": rows15c}, NOW)
check(r7 is not None and r7["level"] == 3 and r7["iv"] == "1h", "1h 胜出 level=3", f"got={r7}")

print("\n【测试7】量比不足（缩量异动）→ 不命中")
rows15e = series("15m", 25, NOW, unclosed=True, closed_move_pct=3.0, last_move_pct=0.2,
                 closed_vol=50.0, last_vol=50.0)
check(sd._l1_screen({"15m": rows15e}, NOW) is None, "缩量异动不命中")

print("\n【测试8】ATR 锚定被判定那一根（截断切片 vs 全量切片结果不同）")
rows_atr = []
t0 = NOW - timedelta(hours=40)
for i in range(40):
    rows_atr.append(bar(t0 + timedelta(hours=i), 100.0,
                        high=100.0 + (10.0 if i >= 30 else 0.2),
                        low=100.0 - (10.0 if i >= 30 else 0.2)))
idx = 29  # 判定根落在「尾部剧变」之前
trunc = sd._atr_stop_pct(rows_atr[:idx + 1], float(rows_atr[idx]["close_px"]))
full = sd._atr_stop_pct(rows_atr, float(rows_atr[-1]["close_px"]))
check(trunc == sd.STOP_PCT_MIN, f"截断切片 → 窄 ATR（夹到下限 {sd.STOP_PCT_MIN}%）", f"trunc={trunc}")
check(full == sd.STOP_PCT_MAX, f"全量切片 → 宽 ATR（夹到上限 {sd.STOP_PCT_MAX}%）", f"full={full}")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)