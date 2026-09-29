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


# ── A2：_brief_top_opportunities（返回 (items, fallback_src_ids)） ──
print("[A2] _brief_top_opportunities")
_opps = [_opp("A", 78), _opp("B", 76), _opp("C", 70), _opp("D", 65), _opp("E", 60)]
_out, _src = mm._brief_top_opportunities(_opps, 3, label="非高确定性档（当日 HIGH 不足）")
check([o["target"] for o in _out] == ["A", "B", "C"], "0 HIGH → 取池内 conv 前 3（不空窗）")
check(all(o.get("display_demoted") is True for o in _out), "兜底项全部打 display_demoted")
check(all(o.get("display_note") for o in _out), "兜底项全部带 display_note（强制标注）")
check(_src == [id(_opps[0]), id(_opps[1]), id(_opps[2])], "返回兜底项原始 id 供 watchlist 剔除")

_opps2 = [_opp("H1", 66, tier="HIGH"), _opp("H2", 64, tier="HIGH"),
          _opp("A", 78), _opp("B", 76), _opp("C", 70), _opp("D", 65)]
_out2, _src2 = mm._brief_top_opportunities(_opps2, 3)
check([o["target"] for o in _out2] == ["H1", "H2", "A", "B", "C"],
      "HIGH 全保留 + 池内 conv 前 3 兜底", str([o["target"] for o in _out2]))
_map = {o["target"]: o for o in _out2}
check(not _map["H1"].get("display_demoted") and not _map["H2"].get("display_demoted"),
      "HIGH 项不打降档标记")
check(_map["A"].get("display_demoted") is True, "兜底项打降档标记")

check(mm._brief_top_opportunities([], 3)[0] == [], "空池 → []")
# 兜底项携带 tier_demote_reason 时进入 display_note
_r, _ = mm._brief_top_opportunities([_opp("X", 75, tier_demote_reason="exempt_unbacktested：未回测")], 3)
check("exempt_unbacktested" in _r[0]["display_note"], "降档原因写入 display_note",
      _r[0]["display_note"])

# N1：HIGH 充足时文案不得说「当日 HIGH 不足」
check("HIGH 不足" in _out[0]["display_note"], "无 HIGH 时用「当日 HIGH 不足」措辞")
check("池内分数靠前（非 HIGH）" in _map["A"]["display_note"]
      and "HIGH 不足" not in _map["A"]["display_note"],
      "有 HIGH 时改用「池内分数靠前（非 HIGH）」（N1 文案不说谎）", _map["A"]["display_note"])

# N3：兜底项是深拷贝，不污染原始（高亮卡）对象
_orig3 = [_opp("Y", 77)]
_orig3[0]["display_demoted"] = False
_c3, _ = mm._brief_top_opportunities(_orig3, 3)
check(_orig3[0].get("display_demoted") is False and "display_note" not in _orig3[0],
      "N3 兜底打标不污染原始对象（deepcopy）")
check(_c3[-1]["display_demoted"] is True, "副本被正确打标")

# ── N2：missing_calibration 与 exempt_* 同口径封顶 ──
print("[N2] missing_calibration 封顶")
mm._SIGNAL_TYPE_CALIBRATION.clear()
mm._SIGNAL_TYPE_CALIBRATION["catalyst"] = {
    "gate": "calibrated_ok", "sample_count": 45, "hit_rate": 0.78,
    "weight_factor": 1.0, "no_high": False, "window_end": "2026-09-25"}
mm._CALIB_LOADED_AT = float("inf")   # 离线：不触 DB
check(mm._exempt_no_high("price_surge") is True,
      "N2 未入表类型（missing_calibration）封顶 MED")
check(mm._exempt_no_high("narrative") is True, "N2 exempt_* 仍封顶")
check(mm._exempt_no_high("catalyst") is False, "N2 calibrated_ok 不封顶（回测背书通过）")

# ── N9：校准表加载失败 ⇒ 保守不封顶（防 DB 抖动静默清零 HIGH） ──
print("[N9] 加载失败保守降级")
_prev_flag = mm._CALIB_LOAD_FAILED
mm._CALIB_LOAD_FAILED = True
check(mm._exempt_no_high("price_surge") is False, "N9 加载失败 ⇒ 不封顶（保守放行）")
check(mm._exempt_no_high("narrative") is False, "N9 加载失败 ⇒ exempt 亦不封顶")
mm._CALIB_LOAD_FAILED = _prev_flag
check(mm._exempt_no_high("price_surge") is True, "N9 恢复后仍正常封顶")

# ── MU6：机会/观察两清单不重叠（兜底原对象必须剔除） ──
print("[MU6] _split_brief_opportunities 不重复")
_opps3 = [_opp("H1", 66, tier="HIGH"), _opp("A", 80), _opp("B", 78),
          _opp("C", 70), _opp("D", 50)]
_items3, _watch3 = mm._split_brief_opportunities(_opps3, 3)
check([o["target"] for o in _items3] == ["H1", "A", "B", "C"], "机会 = HIGH ∪ 池内前 3")
check([o["target"] for o in _watch3] == ["D"], "仅剩余对象进观察清单")
_fb_src = [_opps3[1], _opps3[2], _opps3[3]]
check(all(id(o) not in {id(w) for w in _watch3} for o in _fb_src),
      "兜底项的**原始对象**不在 watchlist（去 src_ids 即红 —— MU6）")
check(id(_opps3[0]) not in {id(w) for w in _watch3}, "HIGH 原对象不在 watchlist")
check(len(_items3) + len(_watch3) == len(_opps3), "机会∪观察 = 全池（不丢不重）")

# ── A2/A3：macro_market brief 接线（源码守卫）──
print("[A2/A3] macro_market 接线")
_mm_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
check("def _brief_top_opportunities(" in _mm_src, "helper 已定义")
check("def _split_brief_opportunities(" in _mm_src and "_split_brief_opportunities(opps" in _mm_src,
      "机会/观察切分走 _split_brief_opportunities（MU6 接线守卫）")
check("risk_signals, 3, label=" in _mm_src, "M4_risks 高危侧同口径对称")
check('"M8_opportunities": _m8_opportunities' in _mm_src, "brief 使用 _m8_opportunities")
check('"M8_watchlist": _m8_watchlist' in _mm_src, "brief 使用 _m8_watchlist（剔除兜底项，避免重复）")
check('"M4_risks": _m4_risks' in _mm_src, "brief 使用 _m4_risks")
check('gate == "missing_calibration"' in _mm_src, "N2 源码含 missing_calibration 同口径判定")
check("_CALIB_LOAD_FAILED" in _mm_src, "N9 源码含加载失败哨兵")

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
check("无满足回测背书的 HIGH" in _html, "N10 无 HIGH 时早报显式标注 HIGH 供给不足")
_brief_hi = dict(_brief)
_brief_hi["M8_opportunities"] = [_opp("CAT", 80, tier="HIGH")]
_html_hi = sdb.render_brief_html(_brief_hi)
check("无满足回测背书的 HIGH" not in _html_hi, "N10 有 HIGH 时不显示供给不足标注")

# ── A1：前端消费 ──
print("[A1] 前端 index.html 消费")
_idx = open(os.path.join(_TEMPLATES, "index.html"), encoding="utf-8").read()
check("o.display_note || o.tier_demote_reason" in _idx, "前端卡片消费 display_note/tier_demote_reason")
check(".signal-demote" in _idx, "前端降档说明样式已定义")

print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}\n{'=' * 60}")
sys.exit(1 if failed else 0)
