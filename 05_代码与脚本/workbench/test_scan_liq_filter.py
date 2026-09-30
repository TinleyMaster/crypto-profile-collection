#!/usr/bin/env python3
"""主池入池流动性闸门单测（工单 SCAN-LIQ-FILTER-001，通道③ 注入测试）。

运行：python test_scan_liq_filter.py

背景：工单新增「绝对流动性闸门」，在**入池前**剔除「成交额/OI 名义价值过低」的符号
（一波流小币交易不了）。阶段 A 默认 `LIQ_FILTER_ENABLED=False`（影子：只记录不拦截）。

覆盖（纯离线，不连库）：
  A) `_liq_verdict` 四档（极低 / 边界 / 极高 / 缺失）各就各位；边界值不差一；
     `unknown` 不误杀（默认放行）；超龄判 `unknown`。
  B) `_sum_vol24_from_klines` 求和口径（含 `quote_vol` NULL / 空 → None）。
  C) `_oi_notional` 取最新桶 + 年龄；无 `oi_usd` → (None, None)。
  D) `_pctl` 分位数。
  E) 源码护栏：开关默认 False；闸门位置在 `_compute_l2` 之后、`signals.append` 之前；
     只落 `low`；落库 SQL 与元组字段数一致；零新 API。
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import scan_daemon as sd  # noqa: E402

_SD_SRC = open(os.path.join(_SCRIPTS, "bin", "scan_daemon.py"), encoding="utf-8").read()

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


UTC = timezone.utc


def main() -> int:
    MIN = sd.LIQ_MIN_QUOTE_VOL_24H_USD
    OI = sd.LIQ_MIN_OI_USD

    # ── A) _liq_verdict 四档 + 边界 + unknown ──
    print("A) _liq_verdict")
    check(sd._liq_verdict(MIN, OI, 1.0) == ("pass", "ok"), "极高两口径 → pass")
    check(sd._liq_verdict(MIN * 100, OI * 100, 0.0) == ("pass", "ok"), "极高含 age=0 → pass")
    check(sd._liq_verdict(MIN - 1, OI, 1.0) == ("low", "vol24_below"),
          "vol24 略低 → low/vol24_below")
    check(sd._liq_verdict(MIN, OI - 1, 1.0) == ("low", "oi_below"),
          "vol24 达标但 OI 略低 → low/oi_below")
    check(sd._liq_verdict(1.0, 1.0, 1.0) == ("low", "vol24_below"),
          "极低 → low（vol24 先命中）")
    # 边界不差一：恰好等于门槛 → pass（判据是 `<` 严格小于）
    check(sd._liq_verdict(MIN, OI, sd.LIQ_OI_MAX_AGE_MIN)[0] == "pass",
          "边界：vol24==门槛 且 age==上限 → pass")
    check(sd._liq_verdict(MIN - 0.01, OI, 1.0)[0] == "low",
          "边界：vol24 差 0.01 → low")
    # unknown：默认放行（不误杀）
    check(sd._liq_verdict(None, OI, 1.0) == ("unknown", "vol24_missing"),
          "vol24 缺失 → unknown/vol24_missing")
    check(sd._liq_verdict(MIN, None, 1.0) == ("unknown", "oi_missing"),
          "OI 缺失 → unknown/oi_missing")
    check(sd._liq_verdict(MIN, OI, None) == ("unknown", "oi_missing"),
          "OI 年龄未知 → unknown/oi_missing")
    check(sd._liq_verdict(MIN, OI, sd.LIQ_OI_MAX_AGE_MIN + 0.01) == ("unknown", "oi_stale"),
          "OI 超龄 → unknown/oi_stale（不误杀）")
    check(sd._liq_verdict(MIN, OI, sd.LIQ_OI_MAX_AGE_MIN)[0] == "pass",
          "OI 恰在上限 → 不算超龄")
    # unknown 优先于 low（数据缺失时不判 low）
    check(sd._liq_verdict(1.0, None, 1.0)[0] == "unknown",
          "unknown 优先于 low（缺失不误杀）")

    # ── B) _sum_vol24_from_klines ──
    print("B) _sum_vol24_from_klines")
    check(sd._sum_vol24_from_klines({"1h": [{"quote_vol": 100}, {"quote_vol": 250}]}) == 350.0,
          "1h quote_vol 求和")
    check(sd._sum_vol24_from_klines({"1h": [{"quote_vol": None}, {"quote_vol": 5}]}) == 5.0,
          "NULL quote_vol 当 0")
    check(sd._sum_vol24_from_klines({"15m": [{"quote_vol": 999}]}) is None,
          "无 1h 条 → None（缺失，不冒充 0）")
    check(sd._sum_vol24_from_klines({}) is None, "空 → None")

    # ── C) _oi_notional ──
    print("C) _oi_notional")
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    rows = [{"oi_usd": 10_000_000.0, "ts": now - timedelta(minutes=50)},
            {"oi_usd": 12_000_000.0, "ts": now - timedelta(minutes=5)}]
    oi, age = sd._oi_notional(rows, now)
    check(oi == 12_000_000.0, "取最新桶 OI（已按 ts 升序）")
    check(abs(age - 5.0) < 1e-9, "年龄 = now - 最新 ts（分钟）")
    check(sd._oi_notional([], now) == (None, None), "无行 → (None, None)")
    check(sd._oi_notional([{"oi_usd": None, "ts": now}], now) == (None, None),
          "oi_usd NULL → (None, None)")

    # ── D) _pctl ──
    print("D) _pctl")
    check(sd._pctl([], 0.5) is None, "空 → None")
    check(sd._pctl([5.0], 0.9) == 5.0, "单值")
    check(sd._pctl([0.0, 10.0], 0.5) == 5.0, "两值中位线性插值")

    # ── E) 源码护栏 ──
    print("E) 源码护栏")
    check(sd.LIQ_FILTER_ENABLED is False, "开关默认 False（阶段 A 影子）")
    check(abs(MIN - 3_000_000.0) < 1e-6 and abs(OI - 3_000_000.0) < 1e-6,
          "门槛 = 300 / 1e-4 = 3,000,000（先验反推）")
    check(sd.LIQ_OI_MAX_AGE_MIN == sd.MAX_OI_BUCKET_AGE_MIN, "超龄口径同 MAX_OI_BUCKET_AGE_MIN")
    check("CREATE TABLE IF NOT EXISTS biz.scan_liq_filter_log" in _SD_SRC, "DDL 建表存在")
    check("_ensure_liq_filter_table(conn)" in _SD_SRC, "主池调用惰性建表")
    check("idx_scan_liq_filter_at" in sd.LIQ_FILTER_INDEX_DDL, "索引 DDL")

    # 闸门位置：_compute_l2 之后、signals.append 之前
    i_l2 = _SD_SRC.index("l2 = _compute_l2(")
    i_gate = _SD_SRC.index("vol24 = _sum_vol24_from_klines")
    i_low = _SD_SRC.index('if liq_v == "low"')
    i_append = _SD_SRC.index("signals.append((")
    check(i_l2 < i_gate < i_low < i_append, "闸门含 low 分桶且在写入之前")
    check(i_l2 < i_gate < i_append, "闸门在 L2 之后、写 scan_signal 之前",
          f"i_l2={i_l2} i_gate={i_gate} i_append={i_append}")
    check("if LIQ_FILTER_ENABLED:" in _SD_SRC and "continue" in
          _SD_SRC[i_gate:i_append], "阶段 B 才 continue（阶段 A 不拦截）")

    # 落库 SQL 字段数 == 元组字段数（防占位符不匹配）
    m = re.search(r"INSERT INTO biz\.scan_liq_filter_log(.*?)filtered_rows",
                  _SD_SRC, re.S)
    check(bool(m), "过滤桶 INSERT 存在")
    if m:
        check(m.group(1).count("%s") == 14,
              "INSERT 占位符 == 14（与元组字段数一致）",
              f"count={m.group(1).count('%s')}")

    # 零新 API：闸门区块不出现 HTTP 调用
    gate_block = _SD_SRC[i_gate:i_append]
    check("_http_get" not in gate_block and "fapi_get" not in gate_block,
          "闸门区块零新 API")
    check("vol24 = _sum_vol24_from_klines" in gate_block,
          "24h 成交额走 asset_klines 求和（DB 原生）")

    print(f"\n[test_scan_liq_filter] {passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
