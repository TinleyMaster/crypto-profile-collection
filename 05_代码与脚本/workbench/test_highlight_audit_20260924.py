#!/usr/bin/env python3
"""高亮信号邮件审计（2026-09-24）处置 · 离线回归护栏。

来源：审计_高亮信号邮件_2026-09-24.md
运行：python workbench/test_highlight_audit_20260924.py（纯离线，不连库、不连网）

覆盖：
  M1 事件强度跨类型可比：flow_pct / mcap_pct 同值同分、单调、封顶一致
  M3 catalyst 补 event_strength（score 主轴）
  M4 邮件对 _ai_downgraded 卡片降级展示 tier（不再 HIGH）
  M6 巨鲸单笔（n_tx<2）事件强度封顶 45（与 KOL 单源一致）
  M7-2 开发活跃倍数主轴 ratio_x + GitHub 卡融合（消除同日同分）
  M7-1 博弈卡类型归位 conflict_game（标签/配额/horizon/邮件标签表）
  N1   GitHub decline 侧事件强度封顶 60（消除温和区/极端区方向语义倒挂）
  N2   催化剂展示分去撞顶 + score 主轴夹 [40,90] + 静默降级留痕
  N3   funding / fng_extreme 补 event_strength
  N4   event_strength 参与排序（池层 _sort_key + 邮件 card_sort_key 末级 tie-break）
  N5   N4 三层收口（AI 终排末级补 es / 池层终排显式 raw→es / 护栏去自证）
  N6   N5 承重加固（终排 es 判别例 / AI 终排 es 层级判别例 / 主卡换源 es 跟随）
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
import ai_signal_analyzer as ai2  # noqa: E402

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
check(mm._event_strength_score("score", 86, {}) == 86, "score 主轴透传 86（带内）")
check(mm._event_strength_score("score", 120, {}) == 90
      and mm._event_strength_score("score", -5, {}) == 40,
      "score 主轴夹取 40-90（N2-b：与其余主轴对齐，消除 es=100 跨类型越界）")
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

# ── M7-2：开发活跃倍数主轴（ratio_x） ──
print("[M7-2] ratio_x 主轴 + GitHub 融合")
check(mm._RATIO_ES_SLOPE == 20.0, "ratio_x 斜率常量为 20.0")
check(mm._event_strength_score("ratio_x", 1.0, {}) == 50, "T1 1.0x → 50（基线）")
check(mm._event_strength_score("ratio_x", 1.5, {}) == 60, "T2 1.5x（触阈）→ 60")
check(mm._event_strength_score("ratio_x", 2.0, {}) == 70, "T3 2.0x → 70")
check(mm._event_strength_score("ratio_x", 3.0, {}) == 90
      and mm._event_strength_score("ratio_x", 5.0, {}) == 90,
      "T4 3.0x/5.0x → 90（封顶）")
check(mm._event_strength_score("ratio_x", 0.5, {}) == 70,
      "T5 decline 0.5x 与 burst 2.0x 对称 → 70")
check(all(mm._event_strength_score("ratio_x", v, {}) == 50
          for v in (None, 0, -1, "abc")),
      "T6 None/0/负/非法 → 50（不惩罚、不崩）")
# 融合口径（与 A1 一致 0.6/0.4）：源码守卫 + 算术恒等
check('_event_strength_score("ratio_x", ratio, t)' in _MACRO_SRC
      and "round(0.6 * conviction + 0.4 * _gh_es)" in _MACRO_SRC,
      "GitHub 卡融合 ratio_x（0.6×conv + 0.4×es）")
check(round(0.6 * 60 + 0.4 * mm._event_strength_score("ratio_x", 2.0, {})) == 64,
      "T7 conv=60 & ratio=2.0 → 64")
check(round(0.6 * 60 + 0.4 * mm._event_strength_score("ratio_x", 2.5, {}))
      != round(0.6 * 60 + 0.4 * mm._event_strength_score("ratio_x", 1.5, {})),
      "T8 同日 2.5x vs 1.5x 不再同分（消除 ASTER/WMETAX 同 67）")
check('"event_strength": _gh_es,' in _MACRO_SRC, "GitHub 卡落 event_strength 字段")

# ── N1：decline 侧封顶（极端停滞不得与极端爆发同分） ──
print("[N1] decline 侧事件强度封顶")
check(mm._GITHUB_DECLINE_ES_CAP == 60, "decline 封顶常量为 60（复验 423fb16：65→60 消除温和区倒挂）")
check(mm._github_event_strength(0.1, "decline", {}) == 60,
      "极端 decline（0.1x）封顶 60（旧码 ratio_x 得 90）")
check(mm._github_event_strength(3.0, "burst", {}) == 90,
      "极端 burst（3.0x）仍 90（不误伤机会侧）")
check(mm._github_event_strength(2.0, "decline", {}) == 60
      and mm._github_event_strength(2.0, "burst", {}) == 70,
      "同倍数 decline(60) < burst(70)")
_dc = round(0.6 * 67 + 0.4 * mm._github_event_strength(0.1, "decline", {}))
_bc = round(0.6 * 67 + 0.4 * mm._github_event_strength(3.0, "burst", {}))
check(_dc < _bc, "语义倒挂消除：极端停滞 conv < 极端爆发 conv", f"decline={_dc} burst={_bc}")
_dc_mild = round(0.6 * 67 + 0.4 * mm._github_event_strength(0.5, "decline", {}))
_bc_mild = round(0.6 * 67 + 0.4 * mm._github_event_strength(1.5, "burst", {}))
check(_dc_mild <= _bc_mild,
      "温和区倒挂也消除：decline 0.5x ≤ burst 1.5x", f"decline={_dc_mild} burst={_bc_mild}")
check("_github_event_strength(ratio, gdir, t)" in _MACRO_SRC,
      "GitHub 循环调用 _github_event_strength")

# ── N2：催化剂展示分失真 / score 主轴越界 / 静默降级 ──
print("[N2] 催化剂分口径 + 静默降级")
check("raw_f * 10" not in _MACRO_SRC,
      "N2-a 旧归一化（raw*10 撞顶）已移除")
check("50.0 + 50.0 * (_raw_pos / (_raw_pos + 30.0))" in _MACRO_SRC,
      "N2-a 改为有界饱和映射（raw=0→50，永不到 100）")
# 映射单调性/不饱和（复算公式）
def _legacy_map(raw):
    p = max(0.0, float(raw))
    return 50.0 + 50.0 * (p / (p + 30.0))
check(_legacy_map(10) < _legacy_map(50) < _legacy_map(120) < 100.0,
      "N2-a 映射单调且永不撞顶 100")
check(round(_legacy_map(51), 1) == 81.5,
      "N2-a ETH（raw=51）→ 81.5，与真实 composite_score 81 量级对齐")
check('logger.warning("_recent_catalyst_decision_targets' in _MACRO_SRC,
      "N2-c 决策查询失败不再静默（logger.warning）")
check('logger.warning("_recent_catalyst_targets' in _MACRO_SRC,
      "N2-c legacy 查询失败不再静默（logger.warning）")

# ── N3：funding / fng_extreme 补事件强度 ──
print("[N3] funding / fng_extreme 事件强度")
check('"event_strength": _raise_es,' in _MACRO_SRC,
      "融资落地卡补 event_strength（金额未披露 → None 不渲染）")
check(_MACRO_SRC.count('"event_strength": _event_strength_score("score", abs(_fg_val - 50) * 2, t),') == 2,
      "恐贪极值两分支（恐惧/贪婪）均补 event_strength")
check(mm._event_strength_score("score", abs(20 - 50) * 2, {}) == 60
      and mm._event_strength_score("score", abs(80 - 50) * 2, {}) == 60,
      "恐贪极端度映射对称（20/80 → 60）")

# ── M8：decline 卡 key_metric 方向词 ──
check('else f"Dev 停滞 ↓{ratio:.1f}x"' in _MACRO_SRC,
      "M8 decline 卡 key_metric 加「停滞 ↓」方向词（burst 保持原样）")

# ── M7-1：博弈卡类型归位 ──
print("[M7-1] conflict_game 类型归位")
check('"signal_type": "conflict_game"' in _MACRO_SRC, "博弈卡 signal_type 改为 conflict_game")
check('"catalyst_events", "P1-2 多空博弈"' not in _MACRO_SRC,
      "博弈卡 related_dims 去掉 catalyst_events（消除双重误导）")
check('"conflict_game": 1,' in _MACRO_SRC, "V2 配额表补 conflict_game:1（不占 catalyst）")
check('"conflict_game":        {"horizon": "short",   "expire_days": 5}' in _MACRO_SRC,
      "horizon map 补 conflict_game short/5")
check(sha.SIGNAL_TYPE_LABEL.get("conflict_game") == "多空博弈",
      "T9 邮件标签表含 conflict_game → 多空博弈（否则徽章露原始 token）")

# T10：conflict_game 独立配额，不挤压 catalyst（构造 3 catalyst + 1 conflict_game）
_hl_in = [
    {"target": f"N{i}", "direction": "long", "signal_type": "catalyst",
     "conviction_score": 70, "conviction_tier": "HIGH", "related_dims": ["catalyst"]}
    for i in range(3)
] + [{"target": "GAME", "direction": "long", "signal_type": "conflict_game",
      "conviction_score": 65, "conviction_tier": "MED", "related_dims": ["P1-2 多空博弈"]}]
_hl_out = mm.select_highlight_signals(_hl_in, max_total=10, min_resonance=1)
_cats = [o for o in _hl_out if o.get("signal_type") == "catalyst"]
_check = [o for o in _hl_out if o.get("signal_type") == "conflict_game"]
check(len(_cats) == 3 and len(_check) == 1 and len(_hl_out) == 4,
      "T10 conflict_game 独立配额，catalyst 仍可选满 3（不互挤）",
      f"catalyst={len(_cats)} conflict_game={len(_check)} total={len(_hl_out)}")

# ── N4：event_strength 参与排序（tie-break） ──
print("[N4] event_strength 参与排序")
check('return (is_high, is_new, resonance, score, raw, es)' in _MACRO_SRC,
      "N4 select_highlight_signals._sort_key 末级加 event_strength（在 raw 之后）")
check('return (is_high, is_new, score, _safe_float(card.get("event_strength")))' in
      open(os.path.join(_SCRIPTS_BIN, "send_highlight_alert.py"), encoding="utf-8").read(),
      "N4 邮件 card_sort_key 末级加 event_strength")


def _tie_opp(tgt, es, score=70, res=1):
    return {"target": tgt, "direction": "long", "signal_type": "narrative",
            "conviction_score": score, "conviction_tier": "MED",
            "related_dims": ["P1-1 叙事榜（市值）"] * res, "event_strength": es}


# 同分同共振：es 高的排前（池层排序）
_hl_tie = mm.select_highlight_signals([_tie_opp("板块A", 60), _tie_opp("板块B", 85)],
                                      max_total=10, min_resonance=1)
check([o["target"] for o in _hl_tie] == ["板块B", "板块A"],
      "N4 池层：同分时事件强度高者排前", str([o["target"] for o in _hl_tie]))
# 分数优先于 es（N5-2：高分卡给低 es、低分卡给高 es，才具判别力——
# 若实现把 es 提到 score 之前，低分高 es 卡会越级，本断言必红）
_hl_hi_score = mm.select_highlight_signals([_tie_opp("板块C", 10, score=80),
                                            _tie_opp("板块D", 99, score=70)],
                                           max_total=10, min_resonance=1)
check([o["target"] for o in _hl_hi_score] == ["板块C", "板块D"],
      "N4 池层：分数仍优先于事件强度（高 es 不得越级压过高分卡）",
      str([o["target"] for o in _hl_hi_score]))
# raw（衰减前原分）优先于 es（N5-2：raw 是保卡下限压平后的还原序，优先级更高；
# 若实现把 es 提到 raw 之前，高 es 低 raw 卡会越级，本断言必红）
_hl_raw_es = mm.select_highlight_signals(
    [{"target": "板块G", "direction": "long", "signal_type": "narrative",
      "conviction_score": 70, "conviction_tier": "MED", "related_dims": ["x"],
      "raw_before_decay": 90, "event_strength": 10},
     _tie_opp("板块H", 90)], max_total=10, min_resonance=1)
check([o["target"] for o in _hl_raw_es] == ["板块G", "板块H"],
      "N4 池层：raw 优先于 es（高 es 不得越级压过高 raw）",
      str([o["target"] for o in _hl_raw_es]))
# 缺 es 不崩、视为 0
_hl_noes = mm.select_highlight_signals(
    [{"target": "板块E", "direction": "long", "signal_type": "narrative",
      "conviction_score": 70, "conviction_tier": "MED", "related_dims": ["x"]},
     _tie_opp("板块F", 50)], max_total=10, min_resonance=1)
check([o["target"] for o in _hl_noes] == ["板块F", "板块E"], "N4 缺 es 视为 0 不崩")

# 邮件层排序键
_k_hi = sha.card_sort_key(({"conviction_tier": "HIGH", "conviction_score": 70,
                            "event_strength": 80}, sha.ALERT_NEW))
_k_lo = sha.card_sort_key(({"conviction_tier": "HIGH", "conviction_score": 70,
                            "event_strength": 60}, sha.ALERT_NEW))
check(_k_hi > _k_lo, "N4 邮件层：同分时 es 高者键更大")
_k_score = sha.card_sort_key(({"conviction_tier": "HIGH", "conviction_score": 80,
                               "event_strength": 0}, sha.ALERT_NEW))
check(_k_score > _k_hi, "N4 邮件层：分数仍优先于 es")
check(sha.card_sort_key(({"conviction_tier": "HIGH", "conviction_score": 70},
                         sha.ALERT_NEW)) == (1, 1, 70, 0.0),
      "N4 邮件层：缺 es 视为 0（不崩、不改变既有排序）")

# ── N5-1：AI 终排（网页高亮榜的最终真源）末级必须带 es ──
# 覆盖 ai_enrich_signals_v2 的 long/short 两支 + V1 回退 ai_enrich_highlight_signals。
# 该层排在 select_highlight_signals 之后并整体覆盖其顺序，缺 es 就等于 N4 在网页端没生效。
_AI_SRC = open(os.path.join(_HERE, "ai_signal_analyzer.py"), encoding="utf-8").read()
check('return (ai_approved, mixed, ai_score, es)' in _AI_SRC,
      "N5-1 v2 long 终排末级加 es")
check('return (ai_approved, mixed, base_score, es)' in _AI_SRC,
      "N5-1 v2 short 终排末级加 es")
check('return (-downgraded, mixed, ai_score, es)' in _AI_SRC
      and 'return (-downgraded, mixed, base_score, es)' in _AI_SRC,
      "N5-1 V1 回退（高亮/高危）末级加 es")


def _enrich_opp(tgt, es, base=70, ai=70):
    # 供 ai_enrich_signals_v2 排序用：同 base 同 AI，仅 es 不同 ⇒ 只有末级 es 能分胜负。
    # target 用非符号（聚合类）⇒ 走 skipped 分支，不触发任何 AI/网络调用。
    return {"target": tgt, "direction": "long", "signal_type": "narrative",
            "conviction_score": base, "conviction_tier": "MED",
            "event_strength": es,
            "ai_analysis_v2": {"overall_score": ai}}


# 隔离外部依赖：load_ai_signal_rules 不读 yaml、analyze_asset_v2 不被调用（非符号 target）
_orig_rules = ai2.load_ai_signal_rules
_orig_analyze = ai2.analyze_asset_v2
ai2.load_ai_signal_rules = lambda *a, **k: {
    "event_driven": set(), "slow_variable": set(),
    "max_review_per_run": 15, "slow_min_resonance": 2,
}
ai2.analyze_asset_v2 = lambda *a, **k: {"error": "test-stub"}
try:
    _ai_tie = ai2.ai_enrich_signals_v2([_enrich_opp("板块I", 10), _enrich_opp("板块J", 90)],
                                       direction="long")
finally:
    ai2.load_ai_signal_rules = _orig_rules
    ai2.analyze_asset_v2 = _orig_analyze
check([o["target"] for o in _ai_tie] == ["板块J", "板块I"],
      "N5-1 AI 终排：同分时 es 高者排前（不再打乱上游 es 序）",
      str([o["target"] for o in _ai_tie]))

# ── N6-1：终排（池层最终排序）的 es 单独承重 ──
# 候选池 _sort_key 的 es 前面还压着 tier / is_new / 共振维数三个分量，它们能把
# 「低 es 卡」先顶上去；只有终排的 es 能把顺序翻回来。此处构造「共振维数」与
# 「es」方向相反的两张卡，专门检验终排 es 是否在做事（M-F 突变体即针对此处）。
_hl_term = mm.select_highlight_signals(
    [_tie_opp("板块M", 10, score=60, res=2), _tie_opp("板块N", 90, score=60, res=1)],
    max_total=10, min_resonance=1)
check([o["target"] for o in _hl_term] == ["板块N", "板块M"],
      "N6-1 终排 es 承重：候选池按共振维数把 M 排前，终排须按 es 翻回 N",
      str([o["target"] for o in _hl_term]))

# ── N6-2：AI 终排 es 的「层级」（须排在各主分量之后）──────────────────
# 仅源码文本守卫不算承重：构造「mixed 并列、ai_score 与 es 方向相反」的并列组，
# 正确实现（ai_score 在 es 前）与把 es 提前的实现给出相反顺序。
def _enrich_opp2(tgt, ai, es, base):
    # mixed = 0.5*ai + 0.5*base ⇒ 取 (ai=80,base=40)/(ai=60,base=60) 使 mixed 并列
    # 同为 60，而 ai_score 与 es 方向相反。非符号 target 走 skipped，不触网络。
    return {"target": tgt, "direction": "long", "signal_type": "narrative",
            "conviction_score": base, "conviction_tier": "MED",
            "event_strength": es, "ai_analysis_v2": {"overall_score": ai}}


ai2.load_ai_signal_rules = lambda *a, **k: {
    "event_driven": set(), "slow_variable": set(),
    "max_review_per_run": 15, "slow_min_resonance": 2,
}
ai2.analyze_asset_v2 = lambda *a, **k: {"error": "test-stub"}
try:
    _ai_layer = ai2.ai_enrich_signals_v2(
        [_enrich_opp2("板块O", 80, 10, 40), _enrich_opp2("板块P", 60, 90, 60)],
        direction="long")
finally:
    ai2.load_ai_signal_rules = _orig_rules
    ai2.analyze_asset_v2 = _orig_analyze
check([o["target"] for o in _ai_layer] == ["板块O", "板块P"],
      "N6-2 AI 终排 es 层级：mixed 并列时 ai_score 先于 es（高 es 不得越级）",
      str([o["target"] for o in _ai_layer]))

# v2 高危支：sort key (ai_approved, mixed, base_score, es)，mixed = 0.5*(100-ai) + 0.5*base。
# 取 (ai=90,base=60)/(ai=60,base=30) 使 mixed 并列 35，base_score 与 es 方向相反。
ai2.load_ai_signal_rules = lambda *a, **k: {
    "event_driven": set(), "slow_variable": set(),
    "max_review_per_run": 15, "slow_min_resonance": 2,
}
ai2.analyze_asset_v2 = lambda *a, **k: {"error": "test-stub"}
try:
    _ai_layer_short = ai2.ai_enrich_signals_v2(
        [_enrich_opp2("板块U", 90, 10, 60), _enrich_opp2("板块V", 60, 90, 30)],
        direction="short")
finally:
    ai2.load_ai_signal_rules = _orig_rules
    ai2.analyze_asset_v2 = _orig_analyze
check([o["target"] for o in _ai_layer_short] == ["板块U", "板块V"],
      "N6-2 AI 终排（高危支）es 层级：mixed 并列时 base_score 先于 es",
      str([o["target"] for o in _ai_layer_short]))

# V1 回退（高亮）：sort key (-downgraded, mixed, ai_score, es)，mixed = 0.4*ai + 0.6*base。
# 取 (ai=90,base=60)/(ai=60,base=80) 使 mixed 并列 72，ai_score 与 es 方向相反。
_orig_merged = ai2._analyze_merged_signal
ai2._analyze_merged_signal = lambda sig, direction: {
    "ai_score": sig.get("_test_ai", 0), "should_highlight": True, "should_risk": True,
}
try:
    _v1_hi = ai2.ai_enrich_highlight_signals([
        {"target": "板块Q", "_test_ai": 90, "conviction_score": 60,
         "conviction_tier": "MED", "event_strength": 10},
        {"target": "板块R", "_test_ai": 60, "conviction_score": 80,
         "conviction_tier": "MED", "event_strength": 90},
    ])
    # V1 回退（高危）：sort key (…, mixed, base_score, es)，mixed = 0.4*(100-ai) + 0.6*base。
    # 取 (ai=90,base=60)/(ai=60,base=40) 使 mixed 并列 40，base_score 与 es 方向相反。
    _v1_risk = ai2.ai_enrich_risk_signals([
        {"target": "板块S", "_test_ai": 90, "conviction_score": 60,
         "conviction_tier": "MED", "event_strength": 10},
        {"target": "板块T", "_test_ai": 60, "conviction_score": 40,
         "conviction_tier": "MED", "event_strength": 90},
    ])
finally:
    ai2._analyze_merged_signal = _orig_merged
check([o["target"] for o in _v1_hi] == ["板块Q", "板块R"],
      "N6-2 V1 高亮终排 es 层级：mixed 并列时 ai_score 先于 es",
      str([o["target"] for o in _v1_hi]))
check([o["target"] for o in _v1_risk] == ["板块S", "板块T"],
      "N6-2 V1 高危终排 es 层级：mixed 并列时 base_score 先于 es",
      str([o["target"] for o in _v1_risk]))

# ── N6-3：主卡换源时 event_strength 跟随 ──
# 同一 target 两条子信号：p1(分60/共振2维/es30) 先被候选池顶到前面成为主卡，
# p2(分80/共振1维/es95) 因分更高触发换源 ⇒ 合并卡 es 须跟随为 95（而非残留 30）。
_swap = mm.select_highlight_signals(
    [{"target": "板块X", "direction": "long", "signal_type": "narrative",
      "conviction_score": 60, "conviction_tier": "MED",
      "related_dims": ["d1", "d2"], "event_strength": 30},
     {"target": "板块X", "direction": "long", "signal_type": "narrative",
      "conviction_score": 80, "conviction_tier": "MED",
      "related_dims": ["d1"], "event_strength": 95}],
    max_total=10, min_resonance=1)
check(len(_swap) == 1 and _swap[0]["conviction_score"] == 80
      and _swap[0]["event_strength"] == 95,
      "N6-3 主卡换源时 event_strength 跟随（不得残留旧主卡值）",
      str([(o["target"], o.get("conviction_score"), o.get("event_strength")) for o in _swap]))

# ── 汇总 ──
print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}\n{'=' * 60}")
sys.exit(1 if failed else 0)
