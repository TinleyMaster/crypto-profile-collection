"""
P0-D：早报「24h 爆仓概况」单元测试

覆盖（对应方案 §4.7 的四条硬约束）：
  1. 批次对齐去重：同一 symbol 跨 5min 桶出现多行时只计最新一行，合计不得加倍
  2. 覆盖率护栏：命中 symbol 数低于约定比例 ⇒ 合计为 None（**不是 0**）
  3. 缺失≠0：列 NULL / 多空分列缺失不产生 0 值
  4. 不进分护栏：组装层浅拷贝，不污染进入 compute_emotion_subscore 的 derivatives 入参
  5. 渲染：口径披露（池内 N 个标的合计、禁止「全网爆仓」）、缺方向时不显示方向

运行: python test_liq_overview_brief.py
"""
import sys
import os
from datetime import datetime, timedelta, timezone

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
# 渲染层在 scripts/bin（P0-D 只在渲染期读快照，不调接口）
_code_root = os.path.dirname(_here)
_scripts_bin = os.path.join(_code_root, "scripts", "bin")
if os.path.isdir(_scripts_bin) and _scripts_bin not in sys.path:
    sys.path.insert(0, _scripts_bin)

from macro_market import (  # noqa: E402
    LIQ_OVERVIEW_MIN_COVERAGE_RATIO,
    summarize_liquidation_snapshot,
    build_derivatives_dimension,
)
from brief_data_model import _WARNING_FIELDS  # noqa: E402
from send_daily_brief import _render_liquidation_row  # noqa: E402

passed = 0
failed = 0


def assert_eq(actual, expected, name):
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        print(f"    期望: {expected}")
        print(f"    实际: {actual}")


_T0 = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)


def _row(sym, minutes, liq24=None, liq1h=1.0, long24=None, short24=None):
    """构造一行爆仓快照（分钟偏移模拟 5min 桶）。"""
    return {
        "symbol": sym,
        "ts": _T0 + timedelta(minutes=minutes),
        "liq_usd_1h": liq1h,
        "liq_usd_4h": 2.0,
        "liq_usd_12h": 3.0,
        "liq_usd_24h": liq24,
        "long_liq_usd_24h": long24,
        "short_liq_usd_24h": short24,
    }


# ════════════════════════════════════════════════════════════
# 1. 批次对齐去重（§4.7 坑 1）
# ════════════════════════════════════════════════════════════
print("\n【测试1】批次对齐去重：每个 symbol 只计该批次最新一行")

_rows_dedup = [
    _row("BTC", 0, liq24=10.0, long24=4.0, short24=6.0),   # 旧桶
    _row("BTC", 5, liq24=20.0, long24=8.0, short24=12.0),  # 旧桶
    _row("BTC", 10, liq24=30.0, long24=10.0, short24=20.0),  # 最新桶 → 只计这行
    _row("ETH", 0, liq24=1.0, long24=0.4, short24=0.6),    # 旧桶
    _row("ETH", 10, liq24=2.0, long24=0.5, short24=1.5),   # 最新桶 → 只计这行
]
out = summarize_liquidation_snapshot(_rows_dedup, universe_24h=2)
assert_eq(out["status"], "ok", "覆盖率达标 → status=ok")
assert_eq(out["liq_usd_24h"], 32.0, "24h 合计 = 30 + 2（不是 10+20+30+1+2=63）")
assert_eq(out["long_24h"], 10.5, "多单 24h 合计 = 10 + 0.5")
assert_eq(out["short_24h"], 21.5, "空单 24h 合计 = 20 + 1.5")
assert_eq(out["symbols_covered"], 2, "命中 symbol 数 = 2（去重后）")
assert_eq(out["ts"], (out["ts"] and max(r["ts"] for r in _rows_dedup).isoformat()),
          "ts = 该批次最新桶时间")


# ════════════════════════════════════════════════════════════
# 2. 覆盖率护栏（§4.7 坑 2）：不足 ⇒ None（不是 0）
# ════════════════════════════════════════════════════════════
print("\n【测试2】覆盖率不足：不返回偏小的假合计")

out2 = summarize_liquidation_snapshot(_rows_dedup, universe_24h=10)
print(f"  覆盖率 = {out2['coverage_ratio']}（下限 {LIQ_OVERVIEW_MIN_COVERAGE_RATIO}）")
assert_eq(out2["status"], "insufficient", "覆盖率不足 → status=insufficient")
assert_eq(out2["liq_usd_24h"], None, "24h 合计为 None")
assert out2["liq_usd_24h"] != 0, "24h 合计不是 0（缺失≠0）"
assert_eq(out2["long_24h"], None, "多单合计同样为 None")
assert_eq(out2["symbols_covered"], 2, "命中数仍如实上报")

# 边界：分母为 0（表内 24h 无任何行）→ error，同样不返回合计
out2b = summarize_liquidation_snapshot(_rows_dedup, universe_24h=0)
assert_eq(out2b["status"], "error", "分母为 0 → status=error")
assert_eq(out2b["liq_usd_24h"], None, "分母为 0 时合计为 None")

# 边界：批次完全无行
out2c = summarize_liquidation_snapshot([], universe_24h=10)
assert_eq(out2c["status"], "error", "批次无行 → status=error")
assert_eq(out2c["liq_usd_24h"], None, "批次无行时合计为 None")


# ════════════════════════════════════════════════════════════
# 3. 缺失列不产生 0（§3.3-1）
# ════════════════════════════════════════════════════════════
print("\n【测试3】列缺失 ⇒ None，绝不补 0")

# 3a. 多空分列缺失（补列前的历史行）：合计照常，方向为 None
_rows_nodir = [
    _row("BTC", 10, liq24=100.0, long24=None, short24=None),
    _row("ETH", 10, liq24=50.0, long24=None, short24=None),
]
out3 = summarize_liquidation_snapshot(_rows_nodir, universe_24h=2)
assert_eq(out3["liq_usd_24h"], 150.0, "合计仍可用（与方向列无关）")
assert_eq(out3["long_24h"], None, "多空分列缺失 → None")
assert_eq(out3["short_24h"], None, "多空分列缺失 → None")
assert out3["long_24h"] != 0 and out3["short_24h"] != 0, "多空分列不为 0"
assert_eq(out3["status"], "partial", "缺方向列 → status=partial")

# 3b. 某币 24h 列本身为 NULL：不得把它当 0 累加（否则是偏小的假合计）
_rows_missing24 = [
    _row("BTC", 10, liq24=100.0, long24=40.0, short24=60.0),
    _row("ETH", 10, liq24=None, long24=20.0, short24=30.0),
]
out3b = summarize_liquidation_snapshot(_rows_missing24, universe_24h=2)
assert_eq(out3b["liq_usd_24h"], None, "有币 24h 列为 NULL → 合计 None（不是 100）")
assert out3b["liq_usd_24h"] != 0, "合计不是 0"
assert_eq(out3b["status"], "partial", "缺窗口列 → status=partial")

# 3c. 1h 列缺失：只影响该档，不影响 24h 合计（占比由渲染层自动省略）
_rows_missing1h = [
    _row("BTC", 10, liq24=100.0, liq1h=None, long24=40.0, short24=60.0),
    _row("ETH", 10, liq24=50.0, liq1h=None, long24=20.0, short24=30.0),
]
out3c = summarize_liquidation_snapshot(_rows_missing1h, universe_24h=2)
assert_eq(out3c["liq_usd_1h"], None, "1h 列缺失 → None")
assert_eq(out3c["liq_usd_24h"], 150.0, "24h 合计不受影响")


# ════════════════════════════════════════════════════════════
# 4. 不进分护栏（§4.7）：组装层浅拷贝，不污染 derivatives 入参
# ════════════════════════════════════════════════════════════
print("\n【测试4】组装层不污染传给 compute_emotion_subscore 的对象")

# 模拟 fetch_binance_derivatives() 的返回（该对象会作为 derivatives= 实参进入打分函数）
fake_derivatives = {"funding_rate": 0.00012, "open_interest": 12345678.0, "status": "ok"}
keys_before = set(fake_derivatives.keys())

merged = build_derivatives_dimension(fake_derivatives, out)
assert_eq("liquidation_24h" in fake_derivatives, False,
          "原 derivatives 对象未被注入 liquidation_24h")
assert_eq(set(fake_derivatives.keys()), keys_before, "原对象键集合未被污染")
assert merged is not fake_derivatives, "组装结果是新对象（浅拷贝）"
assert_eq(merged["liquidation_24h"], out, "新对象上挂有 liquidation_24h")
assert_eq(merged["status"], "ok", "浅拷贝保留原字段")

# 无爆仓数据时也不应凭空造键
merged_none = build_derivatives_dimension(fake_derivatives, None)
assert_eq("liquidation_24h" in merged_none, False, "无数据时不挂 liquidation_24h")

# 登记表：M2_liquidation 必须在辅助模块里（否则健康度检查漏报）
assert_eq("M2_liquidation" in _WARNING_FIELDS, True, "M2_liquidation 已登记到 _WARNING_FIELDS")


# ════════════════════════════════════════════════════════════
# 5. 渲染：口径披露 + 缺失隐藏（缺失≠0）
# ════════════════════════════════════════════════════════════
print("\n【测试5】渲染行：口径披露 / 缺方向不显示方向 / 缺失整行隐藏")

_liq_ok = {
    "status": "ok",
    "liq_usd_1h": 0.05e9,
    "liq_usd_4h": 0.2e9,
    "liq_usd_12h": 1.0e9,
    "liq_usd_24h": 1.5e9,
    "long_24h": 0.3e9,
    "short_24h": 1.2e9,
    "symbols_covered": 527,
    "scope_note": "CoinGlass 全交易所 · 滚动 24h · 5min 快照",
}
row = _render_liquidation_row(_liq_ok)
assert_eq("24h 爆仓 $1.5B" in row, True, "渲染合计（$1.5B）")
assert_eq("多 $300.0M / 空 $1.2B，以空头为主" in row, True, "渲染多空分列 + 方向")
assert_eq("近 1h 占 24h 的 3.3%" in row, True, "1h/24h 属同族滚动窗口，可比较占比")
assert_eq("池内 527 个标的合计" in row, True, "覆盖范围披露为「池内 N 个标的合计」")
assert_eq("全网" in row, False, "不得出现「全网爆仓」")
assert_eq("CoinGlass 全交易所 · 滚动 24h · 5min 快照" in row, True, "口径脚注")

# 缺多空分列：只显示合计，不显示方向、不补 0
_liq_nodir = {"liq_usd_24h": 1e9, "long_24h": None, "short_24h": None,
              "liq_usd_1h": None, "symbols_covered": 500}
row_nodir = _render_liquidation_row(_liq_nodir)
assert_eq("24h 爆仓 $1.0B" in row_nodir, True, "缺方向时仍显示合计")
assert_eq("以多头为主" in row_nodir or "以空头为主" in row_nodir, False, "缺方向不显示方向")
assert_eq("近 1h 占" in row_nodir, False, "缺 1h 列则不显示占比")

# 缺失（旧快照无该 key / 覆盖率不足 / 列 NULL）⇒ 整行隐藏
assert_eq(_render_liquidation_row(None), "", "无该 key → 空串（整行隐藏）")
assert_eq(_render_liquidation_row({}), "", "空 dict → 空串")
assert_eq(_render_liquidation_row({"liq_usd_24h": None, "status": "insufficient"}), "",
          "覆盖率不足 → 空串")
assert_eq(_render_liquidation_row(out2), "", "summarize 的 insufficient 结果 → 空串")


# ════════════════════════════════════════════════════════════
# 总结
# ════════════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"结果: {passed} 通过, {failed} 失败")
print(f"{'='*50}")

sys.exit(0 if failed == 0 else 1)