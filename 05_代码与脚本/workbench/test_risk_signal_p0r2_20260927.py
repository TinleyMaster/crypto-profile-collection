#!/usr/bin/env python3
"""高危信号 P0-R2 护栏：「共振门槛恒 1」+ 缺失的「单源封顶」闸门（离线）。

运行：python workbench/test_risk_signal_p0r2_20260927.py
      （纯离线，不连网、不连库）

来源
----
`audit_高危信号_确定性审计与优化_2026-09-26.md` §三·P0-R2：风险侧
`risk_min_resonance = 1 if ai_v2_enabled_for_init else 1`（**恒 1**）是复制
`select_highlight_signals` 时漏改；且高亮侧的聚合类 HIGH 闸门（P1-C）整块没复制，
故单源风险信号可直冲 HIGH。

验收（审计 §七 表）
----
* 单源 `mvrv_deep_over`（n_confirm=1）→ **不显 HIGH**；
* 「MVRV 高估 + 巨鲸流出」双源 → **可 HIGH**。

判据
----
A. 源码对称：调用处门槛改引高亮侧变量，且「恒 1」写法已消失
B. 源码结构：单源封顶闸门 + 白名单存在，且白名单不含 `mvrv_deep_over`
C. 行为·单源聚合风险 HIGH → 降 MED（卡保留、带 tier_demote_reason）
D. 行为·双源同标的 → 仍 HIGH
E. 行为·硬数据极值白名单（恐贪极值）单源 → 仍 HIGH
F. 行为·币种级 target 单源 HIGH → 降 MED（覆盖币种级，非仅聚合类）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts", "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402

_MACRO_SRC = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()

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


def _risk(tgt, st, score=82, tier="HIGH", direction="short", dims=None, ai_skip=True):
    return {
        "target": tgt, "involved_symbols": [tgt], "direction": direction,
        "signal_type": st, "conviction_score": score, "conviction_tier": tier,
        "related_dims": dims if dims is not None else [st], "ai_skip": ai_skip,
        "asset_id": 9000,
    }


def _by_type(cards, st):
    return next((c for c in cards if c.get("signal_type") == st), None)


print("== A. 源码对称：门槛不再恒 1 ==")
check("risk_min_resonance = hl_min_resonance" in _MACRO_SRC,
      "调用处 `risk_min_resonance = hl_min_resonance`（与高亮侧同源，防再漂移）")
check("1 if ai_v2_enabled_for_init else 1" not in _MACRO_SRC,
      "`1 if ai_v2_enabled_for_init else 1` 恒 1 写法已消失")

print("== B. 源码结构：单源封顶闸门 ==")
check("_RISK_AGG_HIGH_ALLOWLIST = {\"fng_extreme\", \"leverage_extreme\"}" in _MACRO_SRC,
      "风险侧白名单 = {fng_extreme, leverage_extreme}（硬数据极值）")
check("risk_high_min = max(int(min_resonance), 2)" in _MACRO_SRC,
      "HIGH 门槛 = max(min_resonance, 2)（与高亮侧 P1-C 同式）")
check("单源风险信号（" in _MACRO_SRC and "tier_demote_reason" in _MACRO_SRC,
      "降档留痕 tier_demote_reason（可解释，非静默）")
check("\"mvrv_deep_over\"" not in _MACRO_SRC.split("_RISK_AGG_HIGH_ALLOWLIST = {")[1][:80],
      "白名单刻意不含 mvrv_deep_over（审计验收：单源 MVRV 不显 HIGH）")

print("== C. 单源聚合风险 HIGH → 降 MED（卡保留）==")
_c = mm.select_risk_signals([_risk("2 币 MVRV 极度高估", "mvrv_deep_over", score=82)],
                            max_total=8, min_resonance=1)
_card = _by_type(_c, "mvrv_deep_over")
check(_card is not None, "单源 mvrv_deep_over 卡仍在结果中（降档不删卡）")
if _card is not None:
    check(_card.get("conviction_tier") == "MED",
          "tier HIGH → MED", f"got {_card.get('conviction_tier')}")
    check(_card.get("confidence") == "medium",
          "confidence 同步降 medium", f"got {_card.get('confidence')}")
    check("MED" in (_card.get("tier_demote_reason") or ""),
          "带 tier_demote_reason（说明降档原因）", str(_card.get("tier_demote_reason")))

print("== D. 双源同标的 → 仍 HIGH ==")
_c2 = mm.select_risk_signals(
    [_risk("2 币 MVRV 极度高估", "mvrv_deep_over", score=82),
     _risk("2 币 MVRV 极度高估", "whale_flow", score=70)],
    max_total=8, min_resonance=1)
_card2 = _by_type(_c2, "mvrv_deep_over")
check(_card2 is not None and _card2.get("resonance_count") == 2,
      "合并后共振数 = 2", str(_card2.get("resonance_count") if _card2 else None))
check(_card2 is not None and _card2.get("conviction_tier") == "HIGH",
      "双源可 HIGH（增强而非噪声）", str(_card2.get("conviction_tier") if _card2 else None))

print("== E. 硬数据极值白名单单源 → 仍 HIGH ==")
_c3 = mm.select_risk_signals([_risk("恐贪指数极度贪婪", "fng_extreme", score=80)],
                             max_total=8, min_resonance=1)
_card3 = _by_type(_c3, "fng_extreme")
check(_card3 is not None and _card3.get("conviction_tier") == "HIGH",
      "单源 fng_extreme（事实本身）仍 HIGH",
      str(_card3.get("conviction_tier") if _card3 else None))

print("== F. 币种级 target 单源 HIGH → 降 MED ==")
_c4 = mm.select_risk_signals([_risk("SOL", "whale_flow", score=80)],
                             max_total=8, min_resonance=1)
_card4 = _by_type(_c4, "whale_flow")
check(_card4 is not None and _card4.get("conviction_tier") == "MED",
      "币种级单源也封顶 MED（覆盖币种级 target）",
      str(_card4.get("conviction_tier") if _card4 else None))

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)