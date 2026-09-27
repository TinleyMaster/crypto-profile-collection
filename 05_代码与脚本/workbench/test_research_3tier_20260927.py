#!/usr/bin/env python3
"""一键投研页 · 三档确定性（短 S / 中 M / 长 L）离线回归护栏。

方案：04_架构与代码方案/投研页三档确定性可落地方案_2026-09-27.md §6
运行：python workbench/test_research_3tier_20260927.py（纯离线，不连库、不连网）

覆盖（编号对齐方案 §6）：
  1  三档字段非空 + 结构完整
  2  期限标签覆盖率 100%（未标注不静默落档）
  3  口径分离：短期档不被 39.9 压平；同 fixture 下中/长档仍封顶
  4  长期档存在性硬门槛：三项为 0 → not_evaluable，且不被 S/M 档拉升
  5  文本档防骗不回退：28 条空 URL 引用 → 中/长档封顶 39.9，短期档不受影响
  6  闸门标注未校准
  7  前端三档卡渲染（源码级）
  9  源码级断言：短期档不消费 _compute_evidence_stats()（防文本口径混回量化档）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import db_stats as ds  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  → {detail}" if detail else ""))


_DB_SRC = open(os.path.join(_HERE, "db_stats.py"), encoding="utf-8").read()
_HTML_SRC = open(
    os.path.join(_HERE, "templates", "research.html"), encoding="utf-8"
).read()


def _src_of(func_name):
    """取函数源码片段（从 def 行到下一个顶层 def），用于源码级断言。"""
    marker = f"\ndef {func_name}("
    i = _DB_SRC.find(marker)
    if i < 0:
        return ""
    j = _DB_SRC.find("\ndef ", i + len(marker))
    return _DB_SRC[i:j if j > 0 else len(_DB_SRC)]


def _missing(keys_present=(), all_keys=None):
    """构造 missing 清单（含期限/性质字段，与 _compute_missing_materials_inner 同形）。"""
    all_keys = all_keys or [s["key"] for s in ds.RESEARCH_MATERIAL_TYPES]
    out = []
    for k in all_keys:
        p = k in keys_present
        row = {"key": k, "present": p}
        row.update({"horizon": ds._MATERIAL_HORIZON.get(k, (None,))[0]})
        out.append(row)
    return out


# 短期量化输入齐全 + 衍生品 2h 新鲜（PONS 形态：下个解锁 100+ 天外 → 无临近压力）
_SM_OK = {
    "derivatives": {"cvd_ratio_24h": 0.068, "oi_change_24h_pct": -3.12,
                    "funding_rate_pct": 0.005},
    "pressure": {"pressure_score": 26.96, "risk_level": "low"},
    "unlock": {"data_available": True, "next_unlock_date": "2027-01-05",
               "next_unlock_pct": 12.0},
    "data_freshness": {"derivatives": {"age_hours": 2}},
}

# 全论点无有效引用（PONS 实测形态：22 点 0 条可核验引用）
_PONS_THESIS = {
    "thesis": [{"point": f"p{i}", "citations": [{"index": 1, "title": "内部",
                                                 "url": ""}]} for i in range(6)],
    "risks": [{"point": f"r{i}", "citations": [{"index": 1, "url": ""}]} for i in range(4)],
    "dimensions": {
        "valuation": {"points": [{"point": f"v{i}", "citations": [{"index": 1, "url": ""}]}
                                 for i in range(6)]},
        "supply": {"points": [{"point": f"s{i}", "citations": [{"index": 1, "url": ""}]}
                              for i in range(3)]},
        "sentiment": {"points": [{"point": f"e{i}", "citations": [{"index": 1, "url": ""}]}
                                 for i in range(2)]},
        "catalyst": {"points": [{"point": f"c{i}", "citations": [{"index": 1, "url": ""}]}
                                for i in range(1)]},
    },
}

# 三项存在性齐备的资产（长期档可评估）
_L_ALL_PRESENT = ("audit_report", "github_repo", "dao_governance")
# PONS 形态：审计 / 公开代码库 / 治理三项全缺
_L_ALL_ABSENT = ()


# ─────────────────────────────────────────────────────────
# 1 三档字段非空 + 结构完整
# ─────────────────────────────────────────────────────────
print("\n[1] 三档字段非空 + 结构完整")

_r = ds._compute_determinism_3tier(_PONS_THESIS, _SM_OK, _missing(_L_ALL_ABSENT))
check(set(_r.keys()) >= {"s", "m", "l", "composite", "calibration"}, "1 顶层键完整", list(_r))
for _h in ("s", "m", "l"):
    _t = _r[_h]
    check(_t.get("score") is not None or _t.get("tier") == "not_evaluable",
          f"1 {_h} 档 score/tier 有值", _t.get("tier"))
    check(isinstance(_t.get("gate"), dict) and "open" in _t["gate"],
          f"1 {_h} 档含 gate.open")
    check(_t.get("evidence_basis") in ("structured_quant", "text_citation"),
          f"1 {_h} 档标注证据口径", _t.get("evidence_basis"))
    check("notes" in _t and isinstance(_t["notes"], list), f"1 {_h} 档含 notes")
check(_r["s"]["evidence_basis"] == "structured_quant", "1 短期档口径 = 量化数据")
check(_r["m"]["evidence_basis"] == "text_citation", "1 中期档口径 = 文本引用")
check(_r["composite"].get("verdict"), "1 综合结论非空", _r["composite"])

# ─────────────────────────────────────────────────────────
# 2 期限标签覆盖率 100%
# ─────────────────────────────────────────────────────────
print("\n[2] 期限标签覆盖率 100%（未标注不静默落档）")

_KINDS = ("thesis", "risk", "valuation", "supply", "sentiment", "catalyst")
_unmapped_kinds = [k for k in _KINDS if ds._horizon_for_kind(k) not in ("s", "m", "l")]
check(not _unmapped_kinds, "2 全部 kind 均映射到 s/m/l", _unmapped_kinds)
check(ds._horizon_for_kind("__unknown_kind__") is None,
      "2 未知 kind 返回 None（不静默落默认档）")

_pts = ds._iter_thesis_points(_PONS_THESIS)
check(_pts and all(p.get("horizon") in ("s", "m", "l") for p in _pts),
      "2 论点均带 horizon", [p.get("horizon") for p in _pts][:8])
check(_r["unmapped_points"] == 0, "2 未标注论点计数为 0", _r["unmapped_points"])

# 全 21 类投研资料均有期限 + 缺失性质映射
_mkeys = {s["key"] for s in ds.RESEARCH_MATERIAL_TYPES}
check(not (_mkeys - set(ds._MATERIAL_HORIZON)), "2 资料类型期限映射无遗漏",
      sorted(_mkeys - set(ds._MATERIAL_HORIZON)))

# ─────────────────────────────────────────────────────────
# 3 口径分离：短期档不被 39.9 压平，中/长档仍封顶
# ─────────────────────────────────────────────────────────
print("\n[3] 口径分离（S 走量化口径不受封顶；M/L 走文本口径仍封顶）")

_r_eval = ds._compute_determinism_3tier(
    _PONS_THESIS, _SM_OK, _missing(_L_ALL_PRESENT))
check(_r_eval["s"]["score"] >= 70,
      "3 短期档 ≥70（输入齐全 + 2h 新鲜）", _r_eval["s"]["score"])
check(_r_eval["s"]["score"] > ds._ZERO_EVIDENCE_SCORE_CAP,
      "3 短期档已越过零证据封顶", _r_eval["s"]["score"])
check(_r_eval["s"]["tier"] in ("watch", "actionable"),
      "3 短期档档位不为 unusable", _r_eval["s"]["tier"])
check(_r_eval["m"]["score"] <= ds._ZERO_EVIDENCE_SCORE_CAP,
      "3 同 fixture 中期档不越零证据封顶", _r_eval["m"]["score"])
check(_r_eval["m"]["tier"] == "unusable",
      "3 同 fixture 中期档为 unusable", _r_eval["m"]["tier"])
check(_r_eval["l"]["score"] <= ds._ZERO_EVIDENCE_SCORE_CAP,
      "3 同 fixture 长期档不越零证据封顶", _r_eval["l"]["score"])
check(_r_eval["l"]["tier"] == "unusable",
      "3 同 fixture 长期档为 unusable", _r_eval["l"]["tier"])
check(_r_eval["s"]["gate"]["open"] is True,
      "3 短期闸门开启（结构条件全满足）", _r_eval["s"]["gate"])

# 对照：输入不全 → 短期档下降，闸门关闭
_sm_bad = {"derivatives": {}, "pressure": {}, "unlock": {}, "data_freshness": {}}
_r_bad = ds._compute_determinism_3tier(_PONS_THESIS, _sm_bad, _missing(_L_ALL_ABSENT))
check(_r_bad["s"]["score"] < 40, "3 无量化输入时短期档 <40", _r_bad["s"]["score"])
check(_r_bad["s"]["gate"]["open"] is False, "3 无量化输入时短期闸门关闭")
check(any("量化输入未齐" in b for b in _r_bad["s"]["gate"]["blocked_by"]),
      "3 闸门给出未开原因", _r_bad["s"]["gate"]["blocked_by"])

# 临近大额解锁 → 短期闸门关闭（明确反向）
_sm_near = dict(_SM_OK)
_sm_near["unlock"] = {"data_available": True, "next_unlock_date": "2026-10-05",
                      "next_unlock_pct": 12.0}
_r_near = ds._compute_determinism_3tier(_PONS_THESIS, _sm_near, _missing(_L_ALL_ABSENT))
check(_r_near["s"]["gate"]["open"] is False, "3 临近解锁 → 短期闸门关闭")
check(any("临近解锁" in b for b in _r_near["s"]["gate"]["blocked_by"]),
      "3 闸门原因含临近解锁", _r_near["s"]["gate"]["blocked_by"])

# ─────────────────────────────────────────────────────────
# 4 长期档存在性硬门槛（不被 S/M 档拉升）
# ─────────────────────────────────────────────────────────
print("\n[4] 长期档存在性硬门槛（三项全 0 → not_evaluable）")

check(_r["l"]["tier"] == "not_evaluable", "4 PONS 长期档 = not_evaluable", _r["l"]["tier"])
check(_r["l"]["score"] is None, "4 不可评估时 score 为 None", _r["l"]["score"])
check(_r["l"]["existence"]["passed"] == 0, "4 存在性通过数 = 0",
      _r["l"]["existence"])
check(any("存在性" in n for n in _r["l"]["notes"]), "4 notes 注明存在性门槛未过")
check("永久" not in "".join(_r["l"]["notes"]),
      "4 不做「永久不可达」绝对断言（改为存在性门槛表述）")

# S/M 满分不改变 L 结论
_r_perfect = ds._compute_determinism_3tier(_PONS_THESIS, _SM_OK, _missing(_L_ALL_ABSENT))
_r_empty = ds._compute_determinism_3tier(_PONS_THESIS, {}, _missing(_L_ALL_ABSENT))
check(_r_perfect["s"]["score"] != _r_empty["s"]["score"],
      "4 对照组 S 档确实不同（证明拉升路径存在）")
check(_r_perfect["l"]["tier"] == _r_empty["l"]["tier"] == "not_evaluable",
      "4 S/M 档变化不影响 L 档判定")
check(_r_perfect["l"]["existence"] == _r_empty["l"]["existence"],
      "4 L 档存在性结果与 S/M 档无关")

# 三项齐备 → 长期档进入加权
check(_r_eval["l"]["tier"] != "not_evaluable", "4 三项齐备时长期档可评估", _r_eval["l"]["tier"])
check("cross" == _r_eval["l"]["tier"][-5:] or _r_eval["l"]["score"] is not None,
      "4 可评估时长期档有分数", _r_eval["l"]["score"])

# ─────────────────────────────────────────────────────────
# 5 文本档防骗不回退（28 条空 URL 引用）
# ─────────────────────────────────────────────────────────
print("\n[5] 文本档防骗不回退（28 条空 URL 引用 → 中/长档封顶 39.9）")

_inject = {
    "thesis": [{"point": f"p{i}", "citations": [{"index": 1, "url": ""}]} for i in range(12)],
    "risks": [],
    "dimensions": {
        "valuation": {"points": [{"point": f"v{i}", "citations": [{"index": 1, "url": ""}]}
                                 for i in range(10)]},
        "catalyst": {"points": [{"point": f"c{i}", "citations": [{"index": 1, "url": ""}]}
                                for i in range(6)]},
    },
}   # 28 条论点，全部 citations 非空但 url 为空
check(ds._compute_evidence_stats(_inject)["total_points"] == 28,
      "5 注入 28 条论点", ds._compute_evidence_stats(_inject)["total_points"])
_r_inj = ds._compute_determinism_3tier(_inject, _SM_OK, _missing(_L_ALL_PRESENT))
check(_r_inj["m"]["score"] <= ds._ZERO_EVIDENCE_SCORE_CAP,
      "5 中期档不越封顶（非虚高）", _r_inj["m"]["score"])
check(_r_inj["m"]["tier"] == "unusable", "5 中期档 unusable", _r_inj["m"]["tier"])
check(_r_inj["l"]["score"] <= ds._ZERO_EVIDENCE_SCORE_CAP,
      "5 长期档不越封顶（非虚高）", _r_inj["l"]["score"])
check(_r_inj["l"]["tier"] == "unusable", "5 长期档 unusable", _r_inj["l"]["tier"])
check(_r_inj["m"]["evidence"]["weak_cited_points"] > 0,
      "5 弱引用单列计数", _r_inj["m"]["evidence"])
check(_r_inj["s"]["score"] == _r_eval["s"]["score"],
      "5 短期档不受文本引用注入影响", (_r_inj["s"]["score"], _r_eval["s"]["score"]))

# 整体评分卡（旧路径）不回退
_old = ds._compute_determinism(_inject, _SM_OK, _missing(_L_ALL_PRESENT))
check(_old["score"] <= ds._ZERO_EVIDENCE_SCORE_CAP,
      "5 既有整体评分卡不越封顶", _old["score"])
check(_old["tier"] == "unusable", "5 既有整体评分卡仍 unusable", _old["tier"])

# ─────────────────────────────────────────────────────────
# 6 闸门标注未校准
# ─────────────────────────────────────────────────────────
print("\n[6] 闸门与阈值标注未校准")

for _h in ("s", "m", "l"):
    check(_r[_h]["gate"].get("calibration") == "uncalibrated",
          f"6 {_h} 档闸门标注 uncalibrated", _r[_h]["gate"].get("calibration"))
check(_r["calibration"] == "uncalibrated", "6 三档整体标注 uncalibrated")
check(_r["composite"]["calibration"] == "uncalibrated", "6 综合结论标注 uncalibrated")
check("未校准" in _r["calibration_note"], "6 校准说明文案含「未校准」", _r["calibration_note"])

# ─────────────────────────────────────────────────────────
# 7 前端三档卡渲染（源码级：不连浏览器，断言关键钩子存在）
# ─────────────────────────────────────────────────────────
print("\n[7] 前端三档卡渲染（源码级）")

check("analysis.determinism_3tier" in _HTML_SRC, "7 前端消费 analysis.determinism_3tier")
check("_tierCard(t3.s" in _HTML_SRC and "_tierCard(t3.m" in _HTML_SRC
      and "_tierCard(t3.l" in _HTML_SRC,
      "7 三档卡均渲染（短/中/长）")
check("structured_quant" in _HTML_SRC and "text_citation" in _HTML_SRC,
      "7 前端展示证据口径（分档=分来源）")
check("t3.calibration_note" in _HTML_SRC and "闸门阈值未校准" in _HTML_SRC,
      "7 前端标注「未校准」")
check("g.blocked_by" in _HTML_SRC, "7 前端展示闸门未开原因")
check("comp.verdict" in _HTML_SRC and "comp.lines" in _HTML_SRC,
      "7 前端渲染综合结论（不被高分档掩盖）")
check("determinism_gain_by_tier" in _HTML_SRC, "7 缺失项按档给出补全增益")
check("m.horizon_label" in _HTML_SRC and "m.missing_nature_label" in _HTML_SRC,
      "7 缺失项透出期限 + 缺失性质")
check(".t-3t-card" in _HTML_SRC and ".t-3t-composite" in _HTML_SRC,
      "7 三档卡样式已定义")

# ─────────────────────────────────────────────────────────
# 9 源码级断言：短期档不消费文本引用口径
# ─────────────────────────────────────────────────────────
print("\n[9] 源码级断言：短期档不消费 _compute_evidence_stats()")

for _fn in ("_short_term_evidence", "_short_term_gate", "_near_unlock_pressure"):
    _src = _src_of(_fn)
    check(bool(_src), f"9 取到 {_fn} 源码")
    check("_compute_evidence_stats" not in _src,
          f"9 {_fn} 不消费文本引用口径")
    check("_iter_thesis_points" not in _src,
          f"9 {_fn} 不消费论点集合（口径隔离）")

_src_3t = _src_of("_compute_determinism_3tier")
check("_ZERO_EVIDENCE_SCORE_CAP" in _src_3t, "9 三档函数保留零证据封顶常量")
check(_src_3t.count("_ZERO_EVIDENCE_SCORE_CAP") >= 2,
      "9 封顶仅施加于文本档（m/l 两处）", _src_3t.count("_ZERO_EVIDENCE_SCORE_CAP"))
check("short_term_evidence" in _src_3t, "9 三档函数消费短期量化子分")
check("_long_term_existence" in _src_3t, "9 三档函数消费长期存在性门槛")

# 短期档命名不含「确定性」（避免被读成胜率/预测能力）
check("数据完整度" in _r["s"]["title"], "9 短期档命名为「数据完整度」而非「确定性」",
      _r["s"]["title"])

# ─────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"结果：{passed} passed / {failed} failed")
print("=" * 60)
raise SystemExit(1 if failed else 0)