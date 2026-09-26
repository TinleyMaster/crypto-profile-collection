#!/usr/bin/env python3
"""高亮信号确定性审计（2026-09-26）处置 · 离线回归护栏。

来源：audit_高亮信号_确定性审计与优化_2026-09-26.md（刀1 / 刀3 / 刀4 / 刀5 + P2-D）
运行：python workbench/test_highlight_determinacy_20260926.py（纯离线，不连库、不连网）

覆盖：
  刀1  P0-A   conviction 权重默认值与 yaml 真源一致、6 轴和=1.0、死键 cycle/threshold 清除
  刀3  P1-A   _push_opportunity 由 conviction_tier 派生 confidence（单一真源）
  刀3  P1-C   聚合类（非 symbol）target 单源不得 HIGH（硬数据极值白名单除外）
  刀4  P1-B   AI 未背书 → HIGH→MED 强制降级 + 统一标记 ai_endorsed=False（v1/v2）
  刀5  P2-C   decayed_score 跌破 HIGH 门槛 → 显示档位降 MED（只降不升，不丢卡）
  P2-D        死配置 push_confidence_threshold / conviction_weight_cycle 全仓清除
  前端         signalLevelOf 单一真源（消除双轨判层级）+ AI 未背书徽章分支
"""
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm            # noqa: E402
import ai_signal_analyzer as aia     # noqa: E402
import yaml                          # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"    {detail}")


_MACRO_SRC = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
_YAML_SRC = open(os.path.join(_HERE, "market_rules.yaml"), encoding="utf-8").read()
_HTML_SRC = open(os.path.join(_HERE, "templates", "index.html"), encoding="utf-8").read()
_AIA_SRC = open(os.path.join(_HERE, "ai_signal_analyzer.py"), encoding="utf-8").read()
_YAML = yaml.safe_load(_YAML_SRC) or {}
# yaml 侧真源键名是 opportunity_rules（py 侧装载时映射为 opportunity_thresholds）
_YT = _YAML.get("opportunity_rules") or {}

W_KEYS = [
    "conviction_weight_mvrv", "conviction_weight_funding",
    "conviction_weight_netflow", "conviction_weight_stable",
    "conviction_weight_roi", "conviction_weight_catalyst",
]


def _tier_of(o):
    return o.get("conviction_tier")


# ═══════════════ 刀1 / P2-D：权重真源一致 + 死键清除 ═══════════════
print("[刀1·P2-D] conviction 权重默认值 = yaml 真源（6 轴和=1.0，死键已清）")
d = mm.OPPORTUNITY_THRESHOLDS_DEFAULT
w = {k: float(d.get(k, -1)) for k in W_KEYS}
check(len(w) == 6, "6 轴权重键齐全", str(w))
check(abs(sum(w.values()) - 1.0) < 1e-9, f"6 轴和=1.0（实际 {sum(w.values()):.4f}）")
check("conviction_weight_cycle" not in d,
      "死键 conviction_weight_cycle 已从默认表删除")
check("push_confidence_threshold" not in d,
      "死键 push_confidence_threshold 已从默认表删除")
check("conviction_weight_cycle" not in _YT,
      "死键 conviction_weight_cycle 不在 yaml")
check("push_confidence_threshold" not in _YT,
      "死键 push_confidence_threshold 已从 yaml 删除")
for k in W_KEYS:
    check(k in _YT and abs(float(_YT[k]) - w[k]) < 1e-9,
          f"默认与 yaml 同值：{k} = {w[k]}")
check(abs(sum(float(_YT.get(k, -1)) for k in W_KEYS) - 1.0) < 1e-9,
      "yaml 侧 6 轴和=1.0")
check(abs(sum(float(d.get(k, -1)) for k in W_KEYS if k in d)
          - (1.0 - float(d.get("conviction_weight_cycle", 0)))) if "conviction_weight_cycle" in d else True,
      "旧「0.82 权重和」形态不再出现（无 cycle 残留）")

# 回退路径自证：yaml 缺失/加载失败时用默认表，权重和必须仍为 1.0
check(abs(sum(float(d[k]) for k in W_KEYS) - 1.0) < 1e-9,
      "回退默认表时权重和=1.0（P0-A 根因消除）")


# ═══════════════ 刀3 / P1-A：_push_opportunity 派生 confidence ═══════════════
print("[刀3·P1-A] confidence 由 conviction_tier 派生（单一真源）")
_T = dict(mm.OPPORTUNITY_THRESHOLDS_DEFAULT)


def _push(score_raw, pre_conf=None, st="catalyst", tgt="BTC"):
    opp = {"signal_type": st, "target": tgt, "conviction_score": score_raw}
    if pre_conf is not None:
        opp["confidence"] = pre_conf
    exc = []
    mm._push_opportunity(opp, [], exc, _T, cycle_phase="mid", n_confirm=1)
    return opp, exc


hi, exc_hi = _push(95, pre_conf="low")          # 硬编码 low 也应被派生值覆盖
check(_tier_of(hi) == "HIGH" and hi.get("confidence") == "high",
      f"raw 95 → HIGH/high（实得 {_tier_of(hi)}/{hi.get('confidence')}）", str(hi))
med, exc_med = _push(60, pre_conf="high")       # 硬编码 high 也应被压回 medium
check(_tier_of(med) == "MED" and med.get("confidence") == "medium",
      f"raw 60 → MED/medium（实得 {_tier_of(med)}/{med.get('confidence')}）", str(med))
low, exc_low = _push(10)
check(_tier_of(low) == "LOW" and low.get("confidence") == "low",
      f"raw 10 → LOW/low（实得 {_tier_of(low)}/{low.get('confidence')}）")
check(low in exc_low, "LOW 仍进 excluded（过滤语义不变）")
check(hi not in exc_hi and med not in exc_med, "HIGH/MED 不被剔除")

# 派生映射表完整性
for tier_, conf_ in (("HIGH", "high"), ("MED", "medium"), ("LOW", "low")):
    check(f'"{tier_}": "{conf_}"' in _MACRO_SRC.replace("'", '"') or
          f"'{tier_}': '{conf_}'" in _MACRO_SRC,
          f"派生映射含 {tier_}→{conf_}")

# 源码守卫：派生写在 _push_opportunity 内（而非散落各规则）
_push_src = _MACRO_SRC.split("def _push_opportunity(")[1].split("\ndef ")[0]
check('opp["confidence"]' in _push_src and "conviction_tier" in _push_src,
      "派生语句位于 _push_opportunity 函数体内")


# ═══════════════ 刀3 / P1-C：聚合类 HIGH 门槛 ═══════════════
print("[刀3·P1-C] 聚合类 target 单源不得 HIGH（硬数据极值白名单除外）")


def _opp(tgt, st, tier="HIGH", score=88, dims=None, decayed=None):
    o = {"signal_type": st, "target": tgt, "conviction_score": score,
         "conviction_tier": tier, "confidence": "high" if tier == "HIGH" else "medium",
         "related_dims": dims if dims is not None else [st]}
    if decayed is not None:
        o["decayed_score"] = decayed
    return o


def _by_target(res):
    return {r["target"]: r for r in res}


# 单源聚合 HIGH → 降 MED，且卡片保留
r = mm.select_highlight_signals([
    _opp("24h 稳定币净流入 $12B", "stablecoin_inflow", score=92),
], max_total=10)
m = _by_target(r)
agg = m.get("24h 稳定币净流入 $12B")
check(agg is not None, "单源聚合卡不被删除（仍进结果）", str([x["target"] for x in r]))
check(agg and agg.get("conviction_tier") == "MED",
      f"单源聚合 HIGH → MED（实得 {agg and agg.get('conviction_tier')}）")
check(agg and agg.get("confidence") == "medium", "confidence 同步 medium")
check(agg and "tier_demote_reason" in agg, "带降档说明（可解释）")

# 双源聚合 HIGH → 保留
r = mm.select_highlight_signals([
    _opp("24h 稳定币净流入 $12B", "stablecoin_inflow", score=92, dims=["stablecoin_inflow", "narrative"]),
    _opp("24h 稳定币净流入 $12B", "narrative", score=80, dims=["narrative"]),
], max_total=10)
agg = _by_target(r).get("24h 稳定币净流入 $12B")
check(agg and agg.get("conviction_tier") == "HIGH",
      f"双源聚合保留 HIGH（实得 {agg and agg.get('conviction_tier')}）")

# 白名单：硬数据极值单源保留 HIGH
for st in ("fng_extreme", "leverage_extreme", "mvrv_deep_under"):
    r = mm.select_highlight_signals([_opp(f"恐贪指数极度恐惧 ({st})", st, score=95)], max_total=10)
    o = _by_target(r).get(f"恐贪指数极度恐惧 ({st})")
    check(o and o.get("conviction_tier") == "HIGH",
          f"白名单 {st} 单源保留 HIGH")

# 非白名单单源聚合（如 generic 独立源）→ 降 MED
r = mm.select_highlight_signals([_opp("巨鲸单笔转入交易所 $80M", "whale_flow", score=90)], max_total=10)
o = _by_target(r).get("巨鲸单笔转入交易所 $80M")
check(o and o.get("conviction_tier") == "MED", "非白名单单源聚合降 MED")

# 币种 target 不受本闸门影响（单源本就被既有共振筛选拦截；双源 HIGH 保留）
r = mm.select_highlight_signals([
    _opp("BTC", "whale_flow", score=90, dims=["whale_flow"]),
    _opp("BTC", "funding", score=88, dims=["funding"]),
], max_total=10)
o = _by_target(r).get("BTC")
check(o and o.get("conviction_tier") == "HIGH", "币种双源 HIGH 不受聚合闸门影响")
r = mm.select_highlight_signals([_opp("SOL", "whale_flow", score=95)], max_total=10)
check(_by_target(r).get("SOL") is None, "币种单源仍被既有共振筛选拦截（行为不变）")

# min_resonance=1 时聚合 HIGH 门槛仍为 2（不随入参下探）
r = mm.select_highlight_signals(
    [_opp("24h 稳定币净流入 $12B", "stablecoin_inflow", score=92)],
    max_total=10, min_resonance=1)
o = _by_target(r).get("24h 稳定币净流入 $12B")
check(o and o.get("conviction_tier") == "MED",
      f"min_resonance=1 时聚合 HIGH 门槛仍为 2（实得 {o and o.get('conviction_tier')}）")


# ═══════════════ 刀5 / P2-C：显示档位随时间衰减 ═══════════════
print("[刀5·P2-C] decayed_score 跌破 HIGH 门槛 → 降 MED（只降不升，不丢卡）")
HIGH_MIN = float(mm.OPPORTUNITY_THRESHOLDS.get("conviction_high_min", 70))
from datetime import date as _date, timedelta as _td   # noqa: E402

_T = mm.OPPORTUNITY_THRESHOLDS


def _two_src(tgt, main, tier="HIGH", score=95, second_score=88):
    """币种 target 需 ≥2 源才过既有共振筛（否则整卡被剔除，测不到 P2-C）。"""
    a = _opp(tgt, main, tier=tier, score=score, dims=[main])
    b = _opp(tgt, "funding" if main != "funding" else "whale_flow",
             tier=tier, score=second_score, dims=["funding"])
    return [a, b]


def _annotate(opps, days_ago=0):
    """按线上管线打 horizon/decayed_score（apply_horizon 的内层调用，可指定信号日）。"""
    sd = (_date.today() - _td(days=days_ago)).isoformat()
    for o in opps:
        mm._annotate_horizon(o, _T, signal_date=sd)
    return opps


# ① 时间衰减：whale_flow expire_days=5，3 天前的 95 分 → 95*(1-3/5)=57 < 70 → MED
r = mm.select_highlight_signals(_annotate(_two_src("ETH", "whale_flow"), days_ago=3),
                                max_total=10)
o = _by_target(r).get("ETH")
check(o and float(o.get("decayed_score", -1)) < HIGH_MIN and o.get("conviction_tier") == "MED",
      f"时间衰减后 {o and o.get('decayed_score')} < {HIGH_MIN:g} → MED（实得 {o and o.get('conviction_tier')}）",
      str(o and {k: o.get(k) for k in ('conviction_tier', 'decayed_score')}))
check(o and "衰减后" in (o.get("tier_demote_reason") or ""), "带衰减降档说明")
check(o and o.get("conviction_score") == 95,
      "只改显示档位，不回改 conviction_score")

# ② 生产主路径：估值过滤器 ×0.5（线上 P2-C 的实际触发方式）
r = mm.select_highlight_signals(
    mm.apply_horizon_to_opportunities(_two_src("ETH", "whale_flow"), _T, btc_mvrv_pct=90),
    max_total=10)
o = _by_target(r).get("ETH")
check(o and float(o.get("decayed_score", -1)) < HIGH_MIN and o.get("conviction_tier") == "MED",
      f"估值过滤 ×0.5 后 {o and o.get('decayed_score')} < {HIGH_MIN:g} → MED",
      str(o and {k: o.get(k) for k in ('conviction_tier', 'decayed_score')}))
check(o and "估值过热" in (o.get("tier_demote_reason") or ""),
      "降档说明含估值过滤备注")

# ③ 未过估值过滤（mvrv 正常）→ decayed=95 ≥ 门槛 → 保持 HIGH
r = mm.select_highlight_signals(
    mm.apply_horizon_to_opportunities(_two_src("ETH", "whale_flow"), _T, btc_mvrv_pct=None),
    max_total=10)
o = _by_target(r).get("ETH")
check(o and o.get("conviction_tier") == "HIGH", "衰减后仍 ≥ 门槛 → 保持 HIGH")

# ④ 无 decayed_score（未过衰减管线）→ 保持 HIGH
r = mm.select_highlight_signals(_two_src("ETH", "whale_flow"), max_total=10)
o = _by_target(r).get("ETH")
check(o and o.get("conviction_tier") == "HIGH", "无 decayed_score（未过衰减管线）→ 保持 HIGH")

# ⑤ MED 不被二次降级（只降 HIGH，不产生 LOW 丢卡）
r = mm.select_highlight_signals(
    mm.apply_horizon_to_opportunities(
        _two_src("ETH", "whale_flow", tier="MED", score=60, second_score=58),
        _T, btc_mvrv_pct=90),
    max_total=10)
o = _by_target(r).get("ETH")
check(o and o.get("conviction_tier") == "MED", "MED 衰减后仍 MED（不降 LOW、不丢卡）")

# ⑥ 非数值 decayed_score 不炸（排序回退原始分 + P2-C 跳过）
bad = _two_src("ETH", "whale_flow")
for x in bad:
    x["decayed_score"] = "n/a"
r = mm.select_highlight_signals(bad, max_total=10)
o = _by_target(r).get("ETH")
check(o and o.get("conviction_tier") == "HIGH", "decayed_score 非数值 → 不降级不抛错")

# ⑦ 主卡换源时 decayed_score 跟随（合并不能两套口径混算）：
#    排序首位是 88 分新鲜卡，conviction 换成 95 陈旧卡 → decayed 必须一起换成陈旧卡的
mixed = [_opp("ETH", "whale_flow", score=95, dims=["whale_flow"]),
         _opp("ETH", "funding", score=88, dims=["funding"])]
mm._annotate_horizon(mixed[0], _T, signal_date=(_date.today() - _td(days=3)).isoformat())
mm._annotate_horizon(mixed[1], _T, signal_date=_date.today().isoformat())
r = mm.select_highlight_signals(mixed, max_total=10)
o = _by_target(r).get("ETH")
check(o and o.get("conviction_score") == 95 and float(o.get("decayed_score", -1)) < HIGH_MIN,
      "换源后 conviction_score 与 decayed_score 同源",
      str(o and {k: o.get(k) for k in ('conviction_score', 'decayed_score', 'conviction_tier')}))
check(o and o.get("conviction_tier") == "MED", "同源后 P2-C 正确降档（实得 %s）"
      % (o and o.get("conviction_tier")))

# ⑧ 聚合 + 衰减双重降档也只落一次 MED
r = mm.select_highlight_signals(
    [_opp("24h 稳定币净流入 $12B", "stablecoin_inflow", score=92, decayed=40)], max_total=10)
o = _by_target(r).get("24h 稳定币净流入 $12B")
check(o and o.get("conviction_tier") == "MED", "聚合+衰减叠加 → MED（仍不丢卡）")


# ═══════════════ 刀4 / P1-B：AI 未背书强制降级 ═══════════════
print("[刀4·P1-B] AI 未背书 → HIGH→MED + ai_endorsed=False（v1/v2 对称）")
_orig_analyze = aia._analyze_merged_signal
try:
    # v1 高亮：AI 不背书
    aia._analyze_merged_signal = lambda sig, direction="long": {
        "should_highlight": False, "error": None, "ai_score": 40}
    out = aia.ai_enrich_highlight_signals([_opp("BTC", "whale_flow", score=95)])
    o = out[0]
    check(o.get("_ai_downgraded") is True, "v1 高亮：标记 _ai_downgraded")
    check(o.get("conviction_tier") == "MED" and o.get("confidence") == "medium",
          f"v1 高亮 HIGH→MED（实得 {o.get('conviction_tier')}/{o.get('confidence')}）")
    check(o.get("ai_endorsed") is False, "v1 高亮 ai_endorsed=False")

    # v1 高亮：AI 背书 → ai_endorsed=True，档位不动
    aia._analyze_merged_signal = lambda sig, direction="long": {
        "should_highlight": True, "error": None, "ai_score": 88}
    out = aia.ai_enrich_highlight_signals([_opp("BTC", "whale_flow", score=95)])
    o = out[0]
    check(o.get("_ai_downgraded") is None and o.get("ai_endorsed") is True
          and o.get("conviction_tier") == "HIGH",
          "v1 高亮：AI 背书 → 保持 HIGH + ai_endorsed=True")

    # v1 高亮：AI 背书失败（error）不当成"未背书"
    aia._analyze_merged_signal = lambda sig, direction="long": {"error": "timeout"}
    out = aia.ai_enrich_highlight_signals([_opp("BTC", "whale_flow", score=95)])
    o = out[0]
    check(o.get("_ai_downgraded") is None and "ai_endorsed" not in o
          and o.get("conviction_tier") == "HIGH",
          "v1 高亮：AI 调用失败 → 不降级、不写 ai_endorsed")

    # v1 高亮：MED 不二次降级
    aia._analyze_merged_signal = lambda sig, direction="long": {
        "should_highlight": False, "error": None, "ai_score": 30}
    out = aia.ai_enrich_highlight_signals([_opp("BTC", "whale_flow", tier="MED", score=60)])
    o = out[0]
    check(o.get("conviction_tier") == "MED" and o.get("ai_endorsed") is False,
          "v1 高亮：MED 未背书 → 仍 MED（只降一级）")

    # v1 风险：对称
    aia._analyze_merged_signal = lambda sig, direction="short": {
        "should_risk": False, "error": None, "ai_score": 50}
    out = aia.ai_enrich_risk_signals([_opp("ETH", "whale_flow", score=95)])
    o = out[0]
    check(o.get("conviction_tier") == "MED" and o.get("ai_endorsed") is False,
          f"v1 风险 HIGH→MED + ai_endorsed=False（实得 {o.get('conviction_tier')}）")
finally:
    aia._analyze_merged_signal = _orig_analyze

# v2：monkeypatch should_send_to_ai / analyze_asset_v2
_orig_send = aia.should_send_to_ai
_orig_v2 = aia.analyze_asset_v2


def _sig(asset_id=1, tgt="BTC", st="whale_flow", tier="HIGH", score=95):
    return {"asset_id": asset_id, "target": tgt, "signal_type": st,
            "conviction_tier": tier, "conviction_score": score,
            "confidence": "high" if tier == "HIGH" else "medium",
            "all_signals": [{"signal_type": st}]}


try:
    aia.should_send_to_ai = lambda signals, rules=None: (True, "测试直送")
    aia.analyze_asset_v2 = lambda asset_id, all_signals, **kw: {
        "should_highlight": False, "error": None, "overall_score": 42}
    out = aia.ai_enrich_signals_v2([_sig()], direction="long")
    o = out[0]
    check(o.get("_ai_downgraded") is True, "v2 高亮：标记 _ai_downgraded")
    check(o.get("conviction_tier") == "MED" and o.get("confidence") == "medium",
          f"v2 高亮 HIGH→MED（实得 {o.get('conviction_tier')}/{o.get('confidence')}）")
    check(o.get("ai_endorsed") is False, "v2 高亮 ai_endorsed=False")

    aia.analyze_asset_v2 = lambda asset_id, all_signals, **kw: {
        "should_risk": False, "error": None, "overall_score": 55}
    out = aia.ai_enrich_signals_v2([_sig(tgt="ETH")], direction="short")
    o = out[0]
    check(o.get("conviction_tier") == "MED" and o.get("ai_endorsed") is False,
          f"v2 风险 HIGH→MED + ai_endorsed=False（实得 {o.get('conviction_tier')}）")

    aia.analyze_asset_v2 = lambda asset_id, all_signals, **kw: {
        "should_highlight": True, "error": None, "overall_score": 90}
    out = aia.ai_enrich_signals_v2([_sig()], direction="long")
    o = out[0]
    check(o.get("conviction_tier") == "HIGH" and o.get("ai_endorsed") is True
          and not o.get("_ai_downgraded"),
          "v2：AI 背书 → 保持 HIGH + ai_endorsed=True")

    aia.analyze_asset_v2 = lambda asset_id, all_signals, **kw: {"error": "timeout"}
    out = aia.ai_enrich_signals_v2([_sig()], direction="long")
    o = out[0]
    check(o.get("conviction_tier") == "HIGH" and "ai_endorsed" not in o,
          "v2：AI 调用失败 → 不降级、不写 ai_endorsed")

    # 未送 AI 的卡（should_send=False）不被误标 ai_endorsed=False
    aia.should_send_to_ai = lambda signals, rules=None: (False, "不送")
    out = aia.ai_enrich_signals_v2([_sig()], direction="long")
    o = out[0]
    check(o.get("conviction_tier") == "HIGH" and "ai_endorsed" not in o,
          "v2：未送 AI → 档位不动、不写 ai_endorsed")
finally:
    aia.should_send_to_ai = _orig_send
    aia.analyze_asset_v2 = _orig_v2

# 源码守卫：v1 两处 + v2 一处降级写在正确位置
check(_AIA_SRC.count("enriched_sig[\"conviction_tier\"] = \"MED\"") == 2
      or _AIA_SRC.count('enriched_sig["conviction_tier"] = "MED"') == 2,
      "v1 高亮/风险两处均有 HIGH→MED 降级语句")
check('sig["conviction_tier"] = "MED"' in _AIA_SRC, "v2 有 HIGH→MED 降级语句")
check(_AIA_SRC.count("ai_endorsed") >= 6, "ai_endorsed 标记覆盖 v1×2 + v2")


# ═══════════════ 前端：signalLevelOf 单一真源 + AI 未背书徽章 ═══════════════
print("[前端] signalLevelOf 单一真源（消除双轨判层级）")
check("function signalLevelOf" in _HTML_SRC, "signalLevelOf 已定义")
check("o.conviction_tier || o.confidence" not in _HTML_SRC,
      "旧双轨表达式 `o.conviction_tier || o.confidence` 已清除")
check(_HTML_SRC.count("signalLevelOf(") >= 5,
      f"signalLevelOf 被复用（出现 {_HTML_SRC.count('signalLevelOf(')} 次）")
check("ai_endorsed === false" in _HTML_SRC, "新增「⚠ AI 未背书」徽章分支")
check("⚠ AI 未背书" in _HTML_SRC, "徽章文案存在")

# 行为验证：直接用 node 跑真实实现（抽取函数源码）
_m = re.search(r"function signalLevelOf\s*\(o\)\s*\{.*?\n        \}", _HTML_SRC, re.S)
check(bool(_m), "可从模板抽取 signalLevelOf 源码")
if _m:
    js = _m.group(0) + """
const cases = [
  [{conviction_tier:'HIGH', confidence:'low'}, 'high'],
  [{conviction_tier:'MED',  confidence:'high'}, 'medium'],
  [{conviction_tier:'LOW',  confidence:'high'}, 'low'],
  [{confidence:'high'}, 'high'],
  [{confidence:'medium'}, 'medium'],
  [{}, 'medium'],
];
let ok = 0;
for (const [o, exp] of cases) {
  const got = signalLevelOf(o);
  if (got === exp) ok++; else console.log('FAIL', JSON.stringify(o), got, '!=', exp);
}
process.exit(ok === cases.length ? 0 : 1);
"""
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    check(p.returncode == 0, "node 实测 signalLevelOf 优先级 6/6",
          (p.stdout + p.stderr).strip())


# ═══════════════ 汇总 ═══════════════
print("=" * 60)
print(f"通过 {passed} / 失败 {failed}")
print("=" * 60)
sys.exit(0 if failed == 0 else 1)
