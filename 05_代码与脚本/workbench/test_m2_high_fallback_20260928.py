#!/usr/bin/env python3
"""M2 方案 A 离线护栏（HIGH 档位收死与早报空窗，2026-09-28）。

运行：python workbench/test_m2_high_fallback_20260928.py（纯离线，不连库、不连网）

覆盖：
  A2  `macro_market._brief_top_opportunities`：HIGH ∪ 池内 conv 前 N，兜底项打
      display_demoted/display_note，避免降档规则使早报机会段空窗
  A2/A3 macro_market brief 接线（M8_opportunities / M4_risks / M8_watchlist）
  A1  三处消费 tier_demote_reason：高亮邮件 / 早报 / 前端
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS_BIN = os.path.join(_ROOT, "scripts", "bin")
_TEMPLATES = os.path.join(_HERE, "templates")
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


def _opp(tgt, score, tier="MED", **kw):
    d = {"target": tgt, "conviction_score": score, "conviction_tier": tier,
         "direction": "long", "signal_type": "narrative"}
    d.update(kw)
    return d


# ── A2：_brief_top_opportunities ──
print("[A2] _brief_top_opportunities")
_opps = [_opp("A", 78), _opp("B", 76), _opp("C", 70), _opp("D", 65), _opp("E", 60)]
_out = mm._brief_top_opportunities(_opps, 3)
check([o["target"] for o in _out] == ["A", "B", "C"], "0 HIGH → 取池内 conv 前 3（不空窗）")
check(all(o.get("display_demoted") is True for o in _out), "兜底项全部打 display_demoted")
check(all(o.get("display_note") for o in _out), "兜底项全部带 display_note（强制标注）")

_opps2 = [_opp("H1", 66, tier="HIGH"), _opp("H2", 64, tier="HIGH"),
          _opp("A", 78), _opp("B", 76), _opp("C", 70), _opp("D", 65)]
_out2 = mm._brief_top_opportunities(_opps2, 3)
check([o["target"] for o in _out2] == ["H1", "H2", "A", "B", "C"],
      "HIGH 全保留 + 池内 conv 前 3 兜底", str([o["target"] for o in _out2]))
_map = {o["target"]: o for o in _out2}
check(not _map["H1"].get("display_demoted") and not _map["H2"].get("display_demoted"),
      "HIGH 项不打降档标记")
check(_map["A"].get("display_demoted") is True, "兜底项打降档标记")

check(mm._brief_top_opportunities([], 3) == [], "空池 → []")
# 兜底项携带 tier_demote_reason 时进入 display_note
_r = mm._brief_top_opportunities([_opp("X", 75, tier_demote_reason="exempt_unbacktested：未回测")], 3)
check("exempt_unbacktested" in _r[0]["display_note"], "降档原因写入 display_note",
      _r[0]["display_note"])

# ── A2/A3：macro_market brief 接线（源码守卫）──
print("[A2/A3] macro_market 接线")
_mm_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
check("def _brief_top_opportunities(" in _mm_src, "helper 已定义")
check('_brief_top_opportunities(opps, 3' in _mm_src, "M8_opportunities 用 HIGH ∪ 前 3")
check('_brief_top_opportunities(risk_signals, 3' in _mm_src, "M4_risks 高危侧同口径对称")
check('"M8_opportunities": _m8_opportunities' in _mm_src, "brief 使用 _m8_opportunities")
check('"M8_watchlist": _m8_watchlist' in _mm_src, "brief 使用 _m8_watchlist（剔除兜底项，避免重复）")
check('"M4_risks": _m4_risks' in _mm_src, "brief 使用 _m4_risks")

# ── A1：早报渲染消费降档说明 ──
print("[A1] 早报渲染消费降档说明")
_sdb_src = open(os.path.join(_SCRIPTS_BIN, "send_daily_brief.py"), encoding="utf-8").read()
check('display_note") or h.get("tier_demote_reason")' in _sdb_src
      or 'opp.get("display_note") or opp.get("tier_demote_reason")' in _sdb_src,
      "早报机会/高亮卡消费 display_note / tier_demote_reason")
check("降档说明" in _sdb_src, "早报渲染降档说明文案")

_brief = {
    "M0_tldr": {}, "M0_ai_summary": {}, "DIFF": {},
    "M2_flow": {}, "M2_sector_flow": {}, "M2_etf_flow": {},
    "M2_whale_moves": {}, "M2_exchange_flow": {}, "M2_holder_concentration": {},
    "M2_stablecoin": {}, "M6_upcoming_unlocks": {}, "kol_onchain": {},
    "M3_highlights": [{"target": "AI & Big Data", "direction": "long",
                       "conviction_score": 78, "conviction_tier": "MED",
                       "tier_demote_reason": "exempt_unbacktested：该类型从未被回测",
                       "ai_analysis_v2": {"overall_score": 78, "reason_summary": "叙事强"}}],
    "M4_risks": [],
    "M8_opportunities": [_opp("AI & Big Data", 78, display_demoted=True,
                              display_note="非高确定性档（当日 HIGH 不足），按池内分数展示")],
    "M8_watchlist": [],
    "M6_catalyst": {}, "M5_daily_diff": {},
}
try:
    _html = sdb.render_brief_html(_brief)
    _err = None
except Exception as e:  # noqa: BLE001
    _html, _err = "", f"{type(e).__name__}: {e}"
check(_err is None, "render_brief_html 不抛异常", _err)
check("降档说明" in _html, "早报 HTML 含「降档说明」（展示层可解释 78 分 MED）")
check("exempt_unbacktested" in _html or "非高确定性档" in _html,
      "早报带出降档原因（exempt / 兜底标注）")

# ── A1：前端消费 ──
print("[A1] 前端 index.html 消费")
_idx = open(os.path.join(_TEMPLATES, "index.html"), encoding="utf-8").read()
check("o.display_note || o.tier_demote_reason" in _idx, "前端卡片消费 display_note/tier_demote_reason")
check(".signal-demote" in _idx, "前端降档说明样式已定义")

print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}\n{'=' * 60}")
sys.exit(1 if failed else 0)
