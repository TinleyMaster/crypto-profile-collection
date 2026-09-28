#!/usr/bin/env python3
"""U-B：早报「每日变化榜」复活护栏（消费 M5_daily_diff）。

运行：python workbench/test_send_daily_brief_m5.py（纯离线，不连库、不连网）

覆盖：
  1. `_build_daily_diff_brief` 消费嵌套 categories（{cat:{up,down}}）→ 7 榜拍平
     （修复前按扁平形态遍历会命中最外层 available_sectors 抛 AttributeError）
  2. Top5 截断 / ⭐ 高亮 / ⚠️ 高危 标记 / category+direction 透传
  3. 扁平形态兼容（注入/旧形态）
  4. `_fmt_diff_value` 数值口径（量价齐升/赛道轮动=分，其余=%）
  5. `_render_daily_diff_html` 空数据返回空串、正常渲染含 7 榜与标记
  6. `render_brief_html` 接线（模块 1.5，位于模块 2 之前）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS_BIN)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402
import send_daily_brief as sdb  # noqa: E402

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


def _it(sym, val, direction="up", category="price_change_24h"):
    return {"symbol": sym, "metric_value": val, "direction": direction,
            "category": category, "metric_label": "24h 涨跌幅"}


# ── 1. 嵌套 categories → 7 榜拍平（修复前此处抛 AttributeError） ──
print("[1] _build_daily_diff_brief 嵌套形态")
nested = {
    "ok": True,
    "diff_date": "2026-09-27",
    "available_sectors": ["ai", "l2", "defi"],   # 旧实现会在这里 crashe
    "available_tiers": ["top10"],
    "categories": {
        "price_change_24h": {
            "up": [_it("AAA", 12.34), _it("BBB", 9.1), _it("CCC", 8.2),
                   _it("DDD", 7.3), _it("EEE", 6.4), _it("FFF", 5.5)],
            "down": [_it("ZZZ", -8.1, "down")],
        },
        "volume_surge_24h": {"up": [_it("VOL1", 33.3, category="volume_surge_24h")],
                             "down": [_it("SHRINK", 20.0, "down", "volume_surge_24h")]},
        "price_volume_surge": {"up": [_it("PVS1", 88.0, category="price_volume_surge")]},
        "sector_rotation": {"up": [_it("ROT1", 77.5, category="sector_rotation")]},
        # 真实库中解锁抛压落 direction='down'（生成器口径）——M5 须取 down，不能只看 up
        "unlock_7d": {"up": [], "down": [_it("UL1", 621274868.38, "down", "unlock_7d")]},
        "market_cap_mover": {"up": [_it("MC1", -3.2, "down", "market_cap_mover")]},
    },
}
highlights = [{"target": "AAA", "involved_symbols": ["AAA"]}]
risks = [{"target": "ZZZ", "involved_symbols": ["ZZZ"]}]

try:
    m5 = mm._build_daily_diff_brief({"daily_diff_summary": nested}, highlights, risks)
    _raised = None
except Exception as e:  # noqa: BLE001
    m5, _raised = {}, f"{type(e).__name__}: {e}"
check(_raised is None, "嵌套形态不抛异常（旧实现在 available_sectors 处崩）", _raised)
check(list(m5.keys()) == ["价格涨幅榜", "价格跌幅榜", "成交量异动", "量价齐升",
                          "赛道轮动", "即将解锁", "市值变化榜"],
      "7 榜按固定展示顺序产出", str(list(m5.keys())))
check(len(m5.get("价格涨幅榜", [])) == 5, "各榜 Top5 截断（6 条只留 5）")
check(m5["价格涨幅榜"][0]["symbol"] == "AAA" and m5["价格涨幅榜"][0]["is_highlight"] is True,
      "AAA 命中 ⭐ 高亮标记")
check(m5["价格跌幅榜"][0]["symbol"] == "ZZZ" and m5["价格跌幅榜"][0]["is_risk"] is True,
      "ZZZ 命中 ⚠️ 高危标记")
check(m5["价格涨幅榜"][0]["category"] == "price_change_24h"
      and m5["价格涨幅榜"][0]["direction"] == "up",
      "category/direction 透传给渲染层", str(m5["价格涨幅榜"][0]))
check(m5["成交量异动"][0]["symbol"] == "VOL1", "成交量异动取放量侧（up）")

# ── 2. 空 / 异常 → {} ──
print("[2] 空数据兜底")
check(mm._build_daily_diff_brief({"daily_diff_summary": {"ok": True, "categories": {}}},
                                 [], []) == {}, "categories 空 → {}")
check(mm._build_daily_diff_brief({"daily_diff_summary": {"ok": False}}, [], []) == {},
      "无 categories 字段 → {}")

# ── 3. 扁平形态兼容 ──
print("[3] 扁平形态兼容（注入/旧形态）")
flat = mm._build_daily_diff_brief(
    {"daily_diff_summary": {"自定义榜": [_it("X1", 1.0)]}}, [], [])
check(list(flat.keys()) == ["自定义榜"] and flat["自定义榜"][0]["symbol"] == "X1",
      "扁平 {label:[items]} 仍可用（无 category/direction 时用标签兜底）",
      str(flat))

# ── 4. _fmt_diff_value ──
print("[4] 数值口径")
check(sdb._fmt_diff_value({"metric_value": 12.34, "direction": "up",
                           "category": "price_change_24h"}) == "+12.34%", "涨：+12.34%")
check(sdb._fmt_diff_value({"metric_value": -8.1, "direction": "down",
                           "category": "price_change_24h"}) == "-8.10%", "跌：-8.10%")
check(sdb._fmt_diff_value({"metric_value": 88.0, "direction": "up",
                           "category": "price_volume_surge"}) == "88.0 分", "量价齐升：88.0 分")
check(sdb._fmt_diff_value({"metric_value": 621274868.38, "direction": "down",
                           "category": "unlock_7d"}) == "$621.3M",
      "即将解锁：金额（$621.3M），不得渲染成 621274868.38%")
check(sdb._fmt_diff_value({"metric_value": None}) == "—", "缺失 → —（≠0）")
check(sdb._fmt_diff_value({"metric_value": "x", "metric_label": "24h 涨跌幅"}) == "24h 涨跌幅",
      "非数值回退 metric_label")

# ── 5. _render_daily_diff_html ──
print("[5] 渲染层")
check(sdb._render_daily_diff_html({}) == "", "无 M5 → 空串")
check(sdb._render_daily_diff_html({"M5_daily_diff": None}) == "", "M5=None → 空串")
check(sdb._render_daily_diff_html({"M5_daily_diff": {"空榜": []}}) == "", "榜单全空 → 空串")
html_out = sdb._render_daily_diff_html({"M5_daily_diff": m5})
check("每日变化榜" in html_out, "含标题「每日变化榜」")
check(all(lb in html_out for lb in ["价格涨幅榜", "价格跌幅榜", "成交量异动", "量价齐升",
                                    "赛道轮动", "即将解锁", "市值变化榜"]),
      "含 7 个榜名")
check("⭐" in html_out and "⚠️" in html_out, "含 ⭐/⚠️ 标记")
check("+12.34%" in html_out and "88.0 分" in html_out, "含格式化的涨跌幅与综合分")
check("$621.3M" in html_out, "即将解锁显示金额而非百分比")

# ── 6. 接线：模块 1.5（在模块 2 之前） ──
print("[6] render_brief_html 接线")
_src = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
check("html_parts.append(_render_daily_diff_html(brief))" in _src,
      "render_brief_html 消费 _render_daily_diff_html(brief)")
_i_m15 = _src.find("模块 1.5")
_i_m2 = _src.find("模块 2：🏭 赛道轮动")
check(_i_m15 != -1 and _i_m2 != -1 and _i_m15 < _i_m2,
      "每日变化榜插在模块 2（赛道轮动）之前", f"1.5@{_i_m15} / m2@{_i_m2}")

# ── 7. 端到端：render_brief_html 含变化榜且不炸 ──
print("[7] 端到端渲染")
_brief = {
    "M0_tldr": {}, "M0_ai_summary": {}, "DIFF": {},
    "M2_flow": {}, "M2_sector_flow": {}, "M2_etf_flow": {},
    "M2_whale_moves": {}, "M2_exchange_flow": {}, "M2_holder_concentration": {},
    "M2_stablecoin": {}, "M6_upcoming_unlocks": {}, "kol_onchain": {},
    "M3_highlights": [], "M4_risks": [], "M6_catalyst": {},
    "M5_daily_diff": m5,
}
try:
    _html = sdb.render_brief_html(_brief)
    _e2e_err = None
except Exception as e:  # noqa: BLE001
    _html, _e2e_err = "", f"{type(e).__name__}: {e}"
check(_e2e_err is None, "render_brief_html 不抛异常", _e2e_err)
check("每日变化榜" in _html, "端到端 HTML 含「每日变化榜」")
check(_html.find("每日变化榜") < _html.find("赛道轮动") if "赛道轮动" in _html
      else "每日变化榜" in _html,
      "端到端顺序：变化榜在赛道轮动之前")

# ── 汇总 ──
print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}\n{'=' * 60}")
sys.exit(1 if failed else 0)
