#!/usr/bin/env python3
"""高亮信号逻辑审计 2026-10-06 修复回归护栏。

来源：`_audit/高亮信号逻辑审计_2026-10-06.md`（邮件样本 ⚡ 高亮信号提醒 10-06 10:05）。
覆盖「确凿新 bug」2 处：
  · Problem-1（P2 展示口径）：聚合条目「11 币 24h 暴涨」正文仅列 5 币且未披露 →
      `_list_partial_note` 在标题数 > 列出数时追加「共 N 币，仅列头部 M 币」。
  · Problem-2（P1 渲染不一致）：降档说明 4 条 MED 缺失且同类矛盾（成交量异动↔量价齐升、
      ASTER↔WMETAX、AI&BigData/DePIN 叙事↔price_surge）→「无回测背书/单源→封顶 MED」由
      「险些 HIGH 才记」改为**常驻口径**（所有落进 MED 的该类卡一律给说明，已有更具体的
      不覆盖）。
运行：venv/bin/python workbench/test_highlight_logic_fix_20261006.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
_SCRIPTS_SRC = os.path.join(os.path.dirname(_HERE), "scripts", "src")
for _p in (_HERE, _SCRIPTS_BIN, _SCRIPTS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402
import send_highlight_alert as sha  # noqa: E402

_passed = 0
_failed = []


def check(cond, name, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}" + (f" | {detail}" if detail else ""))


_T = dict(mm.OPPORTUNITY_THRESHOLDS_DEFAULT)


def _push(score_raw, st, tgt="BTC", cal=None, t=None):
    """钉死校准条目后跑 _push_opportunity，返回 (opp, excluded, opportunities)（离线）。"""
    mm._SIGNAL_TYPE_CALIBRATION.clear()
    if cal is not None:
        mm._SIGNAL_TYPE_CALIBRATION[st] = cal
    mm._CALIB_LOADED_AT = float("inf")   # 永不触发惰性加载 ⇒ 不连库
    opp = {"signal_type": st, "target": tgt, "conviction_score": score_raw}
    exc, opps = [], []
    mm._push_opportunity(opp, opps, exc, t if t else _T, cycle_phase="mid", n_confirm=1)
    return opp, exc, opps


# ════════════════════════════════════════════════════════
# A. Problem-1：聚合条目「N 币」正文「仅列头部 M 币」披露
# ════════════════════════════════════════════════════════
print("[A] _list_partial_note（标题数 > 列出数 → 显式披露）")
try:
    check(mm._list_partial_note(11, 5) == "；共 11 币，仅列头部 5 币",
          "11 币仅列 5 → 披露后缀")
    check(mm._list_partial_note(5, 5) == "", "5 币全列 → 无后缀")
    check(mm._list_partial_note(5, 6) == "", "列出数 ≥ 总数 → 无后缀（total<=listed）")
    check(mm._list_partial_note(0, 0) == "", "0/0 → 无后缀")
    check(mm._list_partial_note("abc", 5) == "" and mm._list_partial_note(None, 5) == "",
          "非法入参 → 无后缀不炸")
except Exception as _e:
    check(False, "[A] 执行", f"{type(_e).__name__}: {_e}")

print("[A2] 源码守卫：price_surge / price_crash 调用 _list_partial_note")
try:
    _src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
    check("_list_note = _list_partial_note(len(strong), len(syms))" in _src,
          "price_surge 用披露后缀")
    check("_list_note = _list_partial_note(len(crash), len(syms))" in _src,
          "price_crash 用披露后缀")
    check('"{_note}{_list_note}"' in _src or "{_note}{_list_note}" in _src,
          "披露后缀拼接进 trigger_logic")
except Exception as _e:
    check(False, "[A2] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# B. Problem-2a：无回测背书（missing_calibration / exempt）→ MED 也常驻降档说明
# ════════════════════════════════════════════════════════
print("[B] _push_opportunity：MED 卡（非「险些 HIGH」）也开始有缺失校准说明")
try:
    # volume_surge：表内无此类型 → missing_calibration。raw 60 落到 MED（此前无说明）
    opp, exc, opps = _push(60, "volume_surge")
    check(opp.get("conviction_tier") == "MED",
          f"volume_surge raw60 → MED（实得 {opp.get('conviction_tier')}）")
    check("missing_calibration" in (opp.get("tier_demote_reason") or ""),
          "MED 的 volume_surge 也有 missing_calibration 降档说明",
          str(opp.get("tier_demote_reason")))
    check(opp in opps, "卡片保留在 opportunities")

    # narrative：exempt_not_backtestable（样本 0）→ raw 60 MED 也应有 exempt 说明
    opp2, _, opps2 = _push(60, "narrative", tgt="AI & Big Data",
                           cal={"sample_count": 0, "hit_rate": None, "weight_factor": 1.0,
                                "gate": "exempt_not_backtestable", "no_high": False,
                                "window_end": "2026-09-25"})
    check(opp2.get("conviction_tier") == "MED",
          f"narrative raw60 → MED（实得 {opp2.get('conviction_tier')}）")
    check("exempt_unbacktested" in (opp2.get("tier_demote_reason") or ""),
          "MED 的 narrative 也有 exempt_unbacktested 降档说明",
          str(opp2.get("tier_demote_reason")))

    # 有回测背书类型（catalyst calibrated_ok）低分 MED 不应出现「无背书」说明（不误伤）
    opp3, _, _ = _push(60, "catalyst",
                       cal={"sample_count": 45, "hit_rate": 0.7778, "weight_factor": 1.0,
                            "gate": "calibrated_ok", "no_high": False,
                            "window_end": "2026-09-25"})
    check("missing_calibration" not in (opp3.get("tier_demote_reason") or "")
          and "exempt" not in (opp3.get("tier_demote_reason") or ""),
          "有回测背书（calibrated_ok）低分 MED 不标注「无背书」", str(opp3.get("tier_demote_reason")))
except Exception as _e:
    check(False, "[B] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# C. Problem-2b：单源（共振不足）→ MED 也常驻降档说明（ASTER↔WMETAX 矛盾修复）
# ════════════════════════════════════════════════════════
print("[C] select_highlight_signals：单源 MED 卡也有单源封顶说明")
try:

    def _by_target(res):
        return {r["target"]: r for r in res}

    def _opp(tgt, st, tier, score, dims=None):
        return {"signal_type": st, "target": tgt, "conviction_score": score,
                "conviction_tier": tier,
                "confidence": "high" if tier == "HIGH" else "medium",
                "related_dims": dims if dims is not None else [st]}

    # ASTER：开发活跃（github_activity）单源，raw 分本就 MED（此前无说明，仅 WMETAX(高分) 有）
    r = mm.select_highlight_signals(
        [_opp("ASTER", "github_activity", "MED", 60, ["github_repo_activity"])],
        max_total=10, min_resonance=1)
    o = _by_target(r).get("ASTER")
    check(o is not None and o.get("conviction_tier") == "MED", "ASTER 单源 MED 保留")
    check(o and "单源" in (o.get("tier_demote_reason") or ""),
          "ASTER 也有单源封顶降档说明（与 WMETAX 一致）", str(o and o.get("tier_demote_reason")))

    # WMETAX：高分 MED（HIGH→MED 路径）应保持既有说明
    r2 = mm.select_highlight_signals(
        [_opp("WMETAX", "github_activity", "HIGH", 88, ["github_repo_activity"])],
        max_total=10, min_resonance=1)
    o2 = _by_target(r2).get("WMETAX")
    check(o2 and o2.get("conviction_tier") == "MED" and "单源" in (o2.get("tier_demote_reason") or ""),
          "WMETAX 高分降档说明不回归", str(o2 and o2.get("tier_demote_reason")))

    # 双源 MED（两张卡合并 → resonance≥2）不应出现「单源封顶」说明（不误伤）
    r3 = mm.select_highlight_signals(
        [_opp("ETH", "github_activity", "MED", 68, ["github_repo_activity"]),
         _opp("ETH", "etf_flow", "MED", 60, ["etf_flow"])],
        max_total=10, min_resonance=1)
    o3 = _by_target(r3).get("ETH")
    check(o3 and "单源" not in (o3.get("tier_demote_reason") or ""),
          "双源 MED 不标「单源封顶」", str(o3 and o3.get("tier_demote_reason")))
except Exception as _e:
    check(False, "[C] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# D. 源码结构守卫（Problem-2 常驻口径）
# ════════════════════════════════════════════════════════
print("[D] 源码守卫：降档说明常驻（不再只当 tier==HIGH）")
try:
    _mac = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
    _push_src = _mac.split("def _push_opportunity(")[1].split("\ndef ")[0]
    check("if _exempt_no_high(st):" in _push_src
          and "if tier == \"HIGH\":" in _push_src,
          "_push：豁免封顶从「tier==HIGH 才记」改为常驻判断")
    check("if tier == \"MED\":" in _push_src.split("# 审计 2026-10-06")[1]
          if "# 审计 2026-10-06" in _push_src else False,
          "_push：MED 分支也写降档说明")
    _hl_src = _mac.split("def select_highlight_signals(")[1].split("\ndef ")[0]
    near = _hl_src[_hl_src.find("OPT-HL-DETERMINACY-002"):]
    check("if merged.get(\"conviction_tier\") == \"MED\" and not merged.get(\"tier_demote_reason\"):"
          in near, "单源封顶说明在 MED 也写（常驻）", "")
except Exception as _e:
    check(False, "[D] 执行", f"{type(_e).__name__}: {_e}")


# ════════════════════════════════════════════════════════
# E. P1 档位透明：regular MED 自证（距 HIGH 差）+ 邮件档位/AI 综合口径说明
# ════════════════════════════════════════════════════════
print("[E] 档位透明：常规 MED 标注分数与 HIGH 门槛差 + 邮件图例")
try:
    opp, _, _ = _push(60, "catalyst", tgt="JUP",
                      cal={"sample_count": 62, "hit_rate": 0.6, "weight_factor": 1.0,
                           "gate": "calibrated_ok", "no_high": False,
                           "window_end": "2026-09-25"})
    check(opp.get("high_threshold") == 70, "卡片携带 high_threshold（=conviction_high_min）",
          str(opp.get("high_threshold")))

    _card = {"target": "JUP", "signal_type": "catalyst", "conviction_tier": "MED",
             "conviction_score": 59, "high_threshold": 70,
             "direction": "long", "horizon": "medium"}
    _hc = sha.render_card(_card, "hold")
    check("常规 MED" in _hc and "未达 HIGH 门槛（≥70）" in _hc,
          "常规 MED 卡标注 conv 与 HIGH 门槛差", "")
    check("与 AI 综合无关" in _hc, "档位口径明确「与 AI 综合无关」")

    # 有降档说明的 MED 卡 → 只显示降档说明，不叠加「常规 MED」（不重复/不打架）
    _card2 = dict(_card, tier_demote_reason="missing_calibration：该类型（x）未进入回测校准表，默认封顶 MED")
    _hc2 = sha.render_card(_card2, "hold")
    check("降档说明" in _hc2 and "常规 MED" not in _hc2,
          "有降档说明的卡不重复标「常规 MED」")

    # HIGH 卡不出现「常规 MED」
    _hc3 = sha.render_card(dict(_card, conviction_tier="HIGH", conviction_score=72), "upgrade")
    check("常规 MED" not in _hc3, "HIGH 卡不标「常规 MED」")

    # 整封邮件图例
    _html = sha.render_html([(_card, "hold")], "2026-10-06", 1)
    check("档位口径" in _html and "AI 综合低 ≠ 系统分低" in _html and "conviction_score" in _html,
          "邮件含档位/AI 综合口径图例")
    check("**" not in _html, "HTML 无 markdown 强调符（护栏）")

    # high_threshold 经 select_highlight_signals 合并后仍保留（渲染层可读）
    r = mm.select_highlight_signals([dict(opp)], max_total=10, min_resonance=1)
    _merged = _by_target(r).get("JUP")
    check(_merged is not None and _merged.get("high_threshold") == 70,
          "select_highlight_signals 合并后保留 high_threshold",
          str(_merged and _merged.get("high_threshold")))
except Exception as _e:
    check(False, "[E] 执行", f"{type(_e).__name__}: {_e}")


print()
if _failed:
    print(f"[FAIL] {len(_failed)} 项失败：")
    for _f in _failed:
        print(f"   - {_f}")
    sys.exit(1)
print(f"[OK] 全部通过（{_passed}）")
sys.exit(0)