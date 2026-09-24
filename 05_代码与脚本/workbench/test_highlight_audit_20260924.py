#!/usr/bin/env python3
"""高亮信号邮件审计（2026-09-24）处置 · 离线回归护栏。

来源：审计_高亮信号邮件_2026-09-24.md
运行：python workbench/test_highlight_audit_20260924.py（纯离线，不连库、不连网）

覆盖：
  M1 事件强度跨类型可比：flow_pct / mcap_pct 同值同分、单调、封顶一致
  M3 catalyst 补 event_strength（score 主轴）
  M4 邮件对 _ai_downgraded 卡片降级展示 tier（不再 HIGH）
  M6 巨鲸单笔（n_tx<2）事件强度封顶 45（与 KOL 单源一致）
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
import send_highlight_alert as sha  # noqa: E402

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


_MACRO_SRC = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()

# ── M1：跨类型可比 ──
print("[M1] 事件强度跨类型可比（flow_pct ≡ mcap_pct）")
for x in (0.0, 3.0, 5.0, 11.6, 11.9, 12.8, 13.33, 20.0, 50.0, -11.9):
    f = mm._event_strength_score("flow_pct", x, {})
    m = mm._event_strength_score("mcap_pct", x, {})
    check(f == m, f"同值同分：{x} → flow={f} mcap={m}")

check(mm._event_strength_score("flow_pct", 11.9, {})
      == mm._event_strength_score("mcap_pct", 11.9, {}) == 85,
      "审计原例：+11.9% 两口径同为 85（旧码 Base 链 90 / ZK 85）")
check(mm._event_strength_score("flow_pct", 11.6, {})
      < mm._event_strength_score("mcap_pct", 11.9, {}),
      "排序倒置已修：+11.6% < +11.9%（旧码链 ×4 使 +11.6% 反超）")
check(mm._event_strength_score("mcap_pct", 5, {})
      < mm._event_strength_score("mcap_pct", 10, {})
      < mm._event_strength_score("mcap_pct", 13, {})
      <= mm._event_strength_score("mcap_pct", 20, {}),
      "同类型内单调不减")
check(mm._event_strength_score("flow_pct", 0, {}) == 50, "0% → 50（中性）")
check(mm._event_strength_score("flow_pct", 14.0, {}) == 90
      and mm._event_strength_score("flow_pct", 100, {}) == 90,
      "≥40/3≈13.34% 封顶 90（两口径一致）")
check(mm._event_strength_score("flow_pct", -11.9, {})
      == mm._event_strength_score("flow_pct", 11.9, {}),
      "取绝对值：涨跌同强度")
check(mm._event_strength_score("flow_pct", None, {}) == 50
      and mm._event_strength_score("flow_pct", "x", {}) == 50,
      "None / 非法 → 50（不惩罚）")
check(mm._PCT_ES_SLOPE == 3.0 and "_PCT_ES_SLOPE" in _MACRO_SRC,
      "斜率常量为单一真源 ×3（消除 ×4/×3 双曲线）")

# ── M3：catalyst 补 event_strength ──
print("[M3] catalyst 事件强度")
check(mm._event_strength_score("score", 86, {}) == 86, "score 主轴透传 86")
check(mm._event_strength_score("score", 120, {}) == 100
      and mm._event_strength_score("score", -5, {}) == 0,
      "score 主轴夹取 0-100")
check(mm._event_strength_score("score", None, {}) == 50, "score None → 50")
check(_MACRO_SRC.count('_event_strength_score("score", cscore, t)') == 2,
      "催化剂两条路径（决策/回退）均补 event_strength")

# ── M6：巨鲸单笔封顶 ──
print("[M6] 巨鲸单笔（n_tx<2）封顶")
check(mm._whale_event_strength(106_691_685, 1, {}) == 45,
      "单笔 $106.7M → 封顶 45（审计 BURN 原例）")
check(mm._whale_event_strength(106_691_685, 3, {})
      == mm._event_strength_score("usd", 106_691_685, {}) > 45,
      "多笔聚合不封顶（保持金额对数主轴）")
check(mm._whale_event_strength(5_000_000, 1, {}) <= 45,
      "单笔小额也 ≤45")
check(mm._whale_event_strength(106_691_685, None, {}) == 45,
      "n_tx 缺失按单笔保守封顶")
check("_whale_event_strength(usd_total, n_tx, t)" in _MACRO_SRC,
      "巨鲸循环调用 _whale_event_strength")

# ── M4：AI 未背书 → 邮件 tier 降级 ──
print("[M4] 邮件对 AI 未背书卡片降级展示")
_high = sha.render_card({"target": "BURN", "signal_type": "whale_flow",
                         "conviction_tier": "HIGH", "conviction_score": 70}, sha.ALERT_NEW)
check(">HIGH</span>" in _high, "普通 HIGH 卡片仍显示 HIGH 徽章")
_dg = sha.render_card({"target": "BURN", "signal_type": "whale_flow",
                       "conviction_tier": "HIGH", "conviction_score": 70,
                       "_ai_downgraded": True, "_ai_filter_reason": "事件驱动信号触发",
                       "ai_analysis_v2": {"overall_score": 41}}, sha.ALERT_NEW)
check(">HIGH</span>" not in _dg, "AI 未背书卡片不再显示 HIGH 徽章")
check(">MED</span>" in _dg and sha.TIER_COLOR["MED"] in _dg,
      "降级为 MED 且用 MED 配色")
check("AI 未背书" in _dg, "仍披露「AI 未背书」原因")
check(">MED</span>" not in _high, "普通 MED 场景未受影响（回归护栏）")
_med_dg = sha.render_card({"target": "X", "signal_type": "catalyst",
                           "conviction_tier": "MED", "conviction_score": 60,
                           "_ai_downgraded": True}, sha.ALERT_NEW)
check(">MED</span>" in _med_dg, "本就 MED 的降级卡片不变（不误伤）")

# ── 汇总 ──
print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}\n{'=' * 60}")
sys.exit(1 if failed else 0)
