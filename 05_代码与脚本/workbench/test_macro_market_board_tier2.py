#!/usr/bin/env python3
"""变化榜连板派生高亮信号（Tier 2 · FEAT-SIGNAL-SRC-003）离线护栏。

运行：python workbench/test_macro_market_board_tier2.py
      （纯离线，不连网、不连库）

背景
----
变化榜的 `streak_days`（907e913 落地的跨日连板）此前只在前端亮角标，无下游消费。
Tier 2 把它派生为机会池信号：
  - up 侧  → direction=long  → 进 select_highlight_signals（高亮 + ⭐ + 邮件）
  - down 侧 → direction=short → 进 select_risk_signals（高危）
  - 机械信号标 ai_skip=True（P4），ai_enrich_signals_v2 跳过 LLM 增强省成本

判据
----
A. derive_board_opportunities —— 阈值 / 方向映射 / 排序 / 截断 / 中文标签 / ai_skip
B. select_highlight_signals —— 配额、共振门、与同标的信号合并后共振↑
C. select_risk_signals —— down 侧进高危 + 独立配额
D. ai_enrich_signals_v2 —— P4 跳过门（全 ai_skip 跳过；混合卡仍送 AI）
E. 时序结构 —— 注入点必须在 _resolve_symbols_to_asset_ids 之前（P1-1）
F. 口径屏障 —— 连板起点恰为榜单口径变更日时标记 streak_start_ambiguous，
   展示层（强势面板 / 🔥N天 角标）据此不主张强度（FIX-DIFF-STREAK-SEGMENT）
G. D3 结构化字段加权 —— mcap_tier / vol_mcap_ratio（pvs 分位）/
   composite_score（sector_rotation clamp）加成，总 cap 80、缺字段保守 0、
   不改「是否派生」（工单 变化榜D3信号联动+D4质量收口 2026-09-28）
H. U-A 连板增强接早报 —— FEAT-SIGNAL-SRC-001 三段（price_surge/price_crash/
   pvs）消费 streak_days：命中（≥3 且非 ambiguous）加注强度文案 +（涨侧）小幅加成；
   连跌只标注不加成；上限 90/92/88 不变（工单 连板增强接早报 2026-09-28）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts", "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm  # noqa: E402
import ai_signal_analyzer as asa  # noqa: E402
import db_stats as ds  # noqa: E402

_MACRO_SRC = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
_AI_SRC = open(os.path.join(_HERE, "ai_signal_analyzer.py"), encoding="utf-8").read()
_DB_SRC = open(os.path.join(_HERE, "db_stats.py"), encoding="utf-8").read()
_IDX_SRC = open(os.path.join(_HERE, "templates", "index.html"), encoding="utf-8").read()

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


def _diff_cats(rows):
    """rows: [(symbol, streak_days, direction, category, metric_value)]"""
    cats: dict[str, dict] = {}
    for sym, sd, direction, cat, mv in rows:
        cats.setdefault(cat, {"up": [], "down": []})
        cats[cat][direction].append({
            "symbol": sym, "streak_days": sd, "metric_value": mv,
            "streak_first_date": "2026-09-01", "asset_id": 1000 + len(sym),
        })
    return cats


# ── A. derive_board_opportunities ──
print("[A] derive_board_opportunities")
cats = _diff_cats([
    ("AAA", 5, "up", "price_change_24h", 32.5),
    ("BBB", 2, "up", "price_change_24h", 40.0),      # 未达门槛 3
    ("CCC", 3, "up", "price_volume_surge", 88.0),
    ("DDD", 4, "down", "price_change_24h", -25.0),
    ("EEE", 1, "down", "volume_surge_24h", -30.0),   # 未达门槛
])
opps = mm.derive_board_opportunities(cats, streak_threshold=3)
by_target = {o["target"]: o for o in opps}

check(len(opps) == 3, "A1 阈值生效：仅 streak_days>=3 的 3 个标的派生", f"got {len(opps)}")
check("BBB" not in by_target and "EEE" not in by_target, "A2 未达门槛的 BBB/EEE 不派生")

check(by_target["AAA"]["direction"] == "long"
      and by_target["AAA"]["signal_type"] == "diff_streak_up",
      "A3 up 侧 → direction=long / signal_type=diff_streak_up")
check(by_target["DDD"]["direction"] == "short"
      and by_target["DDD"]["signal_type"] == "diff_streak_down",
      "A4 down 侧 → direction=short / signal_type=diff_streak_down（P3）")

check(all(o.get("ai_skip") is True for o in opps),
      "A5 全部机械信号标 ai_skip=True（P4）")
check(all(o["target"] == o["involved_symbols"][0] and len(o["involved_symbols"]) == 1
          for o in opps),
      "A6 target 为单 symbol，involved_symbols 同源（便于同标的合并共振）")

check(by_target["AAA"]["conviction_score"] > by_target["CCC"]["conviction_score"],
      "A7 conviction_score 随连板天数递增（5天 > 3天）",
      f"AAA={by_target['AAA']['conviction_score']} CCC={by_target['CCC']['conviction_score']}")
check(by_target["AAA"]["conviction_score"] <= 80,
      "A8 conviction_score 封顶 80")

dims = by_target["AAA"]["related_dims"]
check(all("price_change_24h" not in d for d in dims) and any("24h涨跌幅" in d for d in dims),
      "A9 related_dims 用中文标签，不透出内部 category 串", str(dims))

# 排序：同方向内按连板天数降序
up_order = [o["target"] for o in opps if o["direction"] == "long"]
check(up_order == ["AAA", "CCC"], "A10 同方向按连板天数降序", str(up_order))

# 截断
many = _diff_cats([(f"S{i}", 4, "up", "price_change_24h", 10.0 + i) for i in range(12)])
check(len(mm.derive_board_opportunities(many, streak_threshold=3, max_per_direction=8)) == 8,
      "A11 max_per_direction 截断生效")

check(mm.derive_board_opportunities({}, streak_threshold=3) == [],
      "A12 空输入安全返回 []")
check(mm.derive_board_opportunities(cats, streak_threshold=0) == [],
      "A13 门槛 <=0 时关闭派生（可当开关用）")

# 方向白名单：unlock_7d 语义相反（up=抛压榜=看空 / down=轻压榜=看多）；
# volume_surge_24h 存在 2026-09-08 榜单扩容造成的结构性伪连板
inverted = _diff_cats([
    ("UUU", 9, "up", "unlock_7d", 12.0),
    ("VVV", 9, "down", "unlock_7d", 0.1),
    ("WWW", 9, "down", "price_volume_surge", 30.0),
    ("XXX", 9, "up", "tvl_surge_24h", 40.0),
    ("ZZZ", 16, "up", "volume_surge_24h", 15.0),
    ("ZZ1", 16, "down", "volume_surge_24h", -30.0),
])
inv_targets = {o["target"] for o in mm.derive_board_opportunities(inverted, streak_threshold=3)}
check(inv_targets == set(),
      "A14 语义相反/伪连板类别不派生（unlock_7d 双向、pvs down、tvl_surge、volume_surge 双向）",
      str(sorted(inv_targets)))

# 白名单内类别仍正常派生
mixed = _diff_cats([("YYY", 4, "up", "price_change_24h", 15.0)])
m = mm.derive_board_opportunities(mixed, streak_threshold=3)
check(len(m) == 1 and m[0]["signal_type"] == "diff_streak_up",
      "A15 白名单内类别（price_change_24h up）正常派生")

# ── B. select_highlight_signals 配额 / 共振 ──
print("[B] select_highlight_signals")


def _board_opp(sym, st="diff_streak_up", score=60, direction="long", ai_skip=True):
    return {"target": sym, "involved_symbols": [sym], "direction": direction,
            "signal_type": st, "conviction_score": score, "conviction_tier": "MED",
            "related_dims": ["连板4天", "24h涨跌幅"], "ai_skip": ai_skip,
            "asset_id": 2000 + len(sym)}


hl_single = mm.select_highlight_signals([_board_opp("AAA")], max_total=10, min_resonance=1)
check(len(hl_single) == 1, "B1 V2 模式（min_resonance=1）孤立连板可入高亮")

four = [_board_opp(s) for s in ("AAA", "BBB", "CCC", "DDD")]
check(len(mm.select_highlight_signals(four, max_total=10, min_resonance=1)) == 3,
      "B2 配额 diff_streak_up=3 生效（4 个标的只出 3 张）")

check(mm.select_highlight_signals([_board_opp("AAA")], max_total=10, min_resonance=2) == [],
      "B3 非 V2（min_resonance=2）孤立连板被共振门丢弃")

merged = mm.select_highlight_signals(
    [_board_opp("BTC", score=60),
     {"target": "BTC", "direction": "long", "signal_type": "catalyst",
      "conviction_score": 78, "conviction_tier": "HIGH", "related_dims": ["catalyst"],
      "asset_id": 1}],
    max_total=10, min_resonance=2)
check(len(merged) == 1 and merged[0].get("resonance_count") == 2,
      "B4 连板与同标的催化剂信号合并 → 共振数 2 → 通过 min_resonance=2（增强而非噪声）",
      str(merged[0].get("resonance_count") if merged else None))

# ── C. select_risk_signals ──
print("[C] select_risk_signals")
risk = mm.select_risk_signals([_board_opp("DDD", st="diff_streak_down", direction="short")],
                              max_total=8, min_resonance=1)
check(len(risk) == 1 and risk[0]["signal_type"] == "diff_streak_down",
      "C1 down 侧连板进高危信号（P3）")
risk4 = mm.select_risk_signals(
    [_board_opp(s, st="diff_streak_down", direction="short") for s in ("AAA", "BBB", "CCC", "DDD")],
    max_total=8, min_resonance=1)
check(len(risk4) == 3, "C2 配额 diff_streak_down=3 生效")

# ── D. ai_enrich_signals_v2 P4 跳过门 ──
print("[D] ai_enrich_signals_v2 P4 跳过门")
_orig_rules = asa.load_ai_signal_rules
_orig_analyze = asa.analyze_asset_v2
_analyzed: list[int] = []
try:
    asa.load_ai_signal_rules = lambda *a, **k: {
        "max_review_per_run": 10, "event_driven": set(),
        "slow_variable": set(), "slow_min_resonance": 2,
    }
    asa.analyze_asset_v2 = lambda aid, sigs: (
        _analyzed.append(aid) or {"overall_score": 60, "should_highlight": True}
    )

    mech = asa.ai_enrich_signals_v2([_board_opp("AAA")], direction="long", max_ai_review=10)
    check(len(mech) == 1 and "ai_analysis_v2" not in mech[0],
          "D1 全 ai_skip 的机械卡不进 AI 增强")
    check("机械信号" in (mech[0].get("_ai_filter_reason") or ""),
          "D2 跳过原因用用户友好文案", str(mech[0].get("_ai_filter_reason")))
    check(_analyzed == [], "D3 机械卡未触发 analyze_asset_v2（零 LLM 成本）")

    mixed = _board_opp("BTC")
    mixed["all_signals"] = [
        dict(mixed, ai_skip=True),
        {"target": "BTC", "direction": "long", "signal_type": "catalyst",
         "conviction_score": 78, "ai_skip": False},
    ]
    asa.ai_enrich_signals_v2([mixed], direction="long", max_ai_review=10)
    check(_analyzed == [mixed["asset_id"]],
          "D4 混合卡（含非机械子信号）仍送 AI（连板起共振增强作用）",
          str(_analyzed))
finally:
    asa.load_ai_signal_rules = _orig_rules
    asa.analyze_asset_v2 = _orig_analyze

check("if all_signals and all(s.get(\"ai_skip\") for s in all_signals):" in _AI_SRC,
      "D5 源码含 P4 跳过门（all(...) 语义：混合卡不跳过）")

# ── E. 时序结构 ──
print("[E] 注入时序")
_inject_pos = _MACRO_SRC.find("derive_board_opportunities(\n                diff_cats")
if _inject_pos == -1:
    _inject_pos = _MACRO_SRC.find("derive_board_opportunities(")
_resolve_pos = _MACRO_SRC.find("symbol_to_asset = _resolve_symbols_to_asset_ids(all_symbols)")
_hl_pos = _MACRO_SRC.find("highlights = select_highlight_signals(")
check(_inject_pos != -1, "E1 存在 derive_board_opportunities 注入点")
check(-1 < _inject_pos < _resolve_pos < _hl_pos,
      "E2 派生机会入池早于 asset_id 解析、更早于高亮精选（P1-1 时序）",
      f"inject@{_inject_pos} resolve@{_resolve_pos} highlights@{_hl_pos}")

# ── F. 口径屏障（FIX-DIFF-STREAK-SEGMENT）──
print("[F] 连板口径屏障")


class _FakeCur:
    """最小游标替身：只回放任给的连板行，避免测试连库。"""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params):
        self._sql = sql

    def fetchall(self):
        return self._rows


check(ds.DIFF_STREAK_CALIBER_BARRIERS.get("volume_surge_24h") == frozenset({"2026-09-08"})
      and ds.DIFF_STREAK_CALIBER_BARRIERS.get("price_change_24h") == frozenset({"2026-09-08"}),
      "F1 口径变更日常量：volume_surge_24h / price_change_24h 均为 2026-09-08（LIMIT 扩容生效日）",
      str(ds.DIFF_STREAK_CALIBER_BARRIERS))

_rows = [
    # 起点恰为口径变更日 → 连板天数=口径年龄，标 ambiguous
    {"asset_id": 7289, "category": "volume_surge_24h", "direction": "up",
     "streak_days": 16, "first_date": "2026-09-08"},
    # 起点晚于口径变更日 → 口径稳定期内的真实连板
    {"asset_id": 100, "category": "volume_surge_24h", "direction": "up",
     "streak_days": 4, "first_date": "2026-09-20"},
    # 同样起点，但类别无口径变更记录 → 不误伤
    {"asset_id": 200, "category": "unlock_7d", "direction": "down",
     "streak_days": 8, "first_date": "2026-09-08"},
]
_sm = ds._fetch_streak_map(_FakeCur(_rows), "2026-09-23")
check(_sm[(7289, "volume_surge_24h", "up")]["ambiguous_start"] is True,
      "F2 起点恰为口径变更日 → ambiguous_start=True")
check(_sm[(100, "volume_surge_24h", "up")]["ambiguous_start"] is False,
      "F3 起点晚于变更日（口径稳定期）→ ambiguous_start=False")
check(_sm[(200, "unlock_7d", "down")]["ambiguous_start"] is False,
      "F4 无口径变更记录的类别不被误伤")
check(_sm[(100, "volume_surge_24h", "up")]["streak_days"] == 4
      and _sm[(7289, "volume_surge_24h", "up")]["streak_days"] == 16,
      "F5 屏障只加标记、不改写原始连板天数（不销毁真实数据）")

check('"streak_start_ambiguous": bool(streak.get("ambiguous_start"))' in _DB_SRC,
      "F6 get_daily_diff_summary 向接口透出 streak_start_ambiguous")
check("if (item.streak_start_ambiguous) return;" in _IDX_SRC,
      "F7 强势面板「连板榜」排除 ambiguous（口径年龄不是强度，跨标的无区分度）")
check("item.streak_start_ambiguous" in _IDX_SRC
      and "该日为榜单口径变更日，起点之前无可比数据" in _IDX_SRC,
      "F8 🔥N天 角标保留事实但在 tooltip 显式标注不可比")

# ── G. D3 结构化字段加权 ──
print("[G] D3 结构化字段加权")


def _item(sym, sd=3, mv=10.0, mcap_tier=None, detail=None):
    it = {"symbol": sym, "streak_days": sd, "metric_value": mv,
          "streak_first_date": "2026-09-01", "asset_id": 3000 + len(sym)}
    if mcap_tier is not None:
        it["mcap_tier"] = mcap_tier
    if detail is not None:
        it["detail"] = detail
    return it


def _cats(cat, direction, items):
    return {cat: {direction: items}}


# G1 mcap_tier 加成（同类别同连板，仅分层不同）
_g1 = {o["target"]: o for o in mm.derive_board_opportunities(_cats("price_change_24h", "up", [
    _item("HA", mcap_tier="top10"), _item("HB", mcap_tier="top1000"),
]), streak_threshold=3)}
check(_g1["HA"]["conviction_score"] - _g1["HB"]["conviction_score"] == 6,
      "G1 mcap_tier 加成：top10 比 top1000 高 6 分",
      f"HA={_g1['HA']['conviction_score']} HB={_g1['HB']['conviction_score']}")
check(_g1["HA"]["board_score_bonus"]["mcap_tier"] == 6
      and _g1["HB"]["board_score_bonus"]["mcap_tier"] == 0,
      "G1b 溯源字段 board_score_bonus.mcap_tier 正确")

# G2 vol_mcap_ratio 批次分位加成（n=4：top25%→+6 / 前50%→+3 / 其余 0）
_g2 = {o["target"]: o for o in mm.derive_board_opportunities(_cats("price_volume_surge", "up", [
    _item("V1", detail={"vol_mcap_ratio": 0.1}), _item("V2", detail={"vol_mcap_ratio": 0.5}),
    _item("V3", detail={"vol_mcap_ratio": 1.0}), _item("V4", detail={"vol_mcap_ratio": 2.0}),
]), streak_threshold=3)}
check(_g2["V4"]["board_score_bonus"]["vol_mcap"] == 6, "G2 vol top25% → +6")
check(_g2["V3"]["board_score_bonus"]["vol_mcap"] == 3, "G2b vol 前50% → +3")
check(_g2["V2"]["board_score_bonus"]["vol_mcap"] == 0
      and _g2["V1"]["board_score_bonus"]["vol_mcap"] == 0, "G2c vol 后50% → 0")

# G3 composite_score clamp 加成（仅 sector_rotation；(cs-50)/5，clamp [0,8]）
_g3 = {o["target"]: o for o in mm.derive_board_opportunities(_cats("sector_rotation", "up", [
    _item("S1", detail={"composite_score": 50}), _item("S2", detail={"composite_score": 70}),
    _item("S3", detail={"composite_score": 30}), _item("S4", detail={"composite_score": 100}),
]), streak_threshold=3)}
check(_g3["S1"]["board_score_bonus"]["sector"] == 0, "G3 composite=50 → +0")
check(_g3["S2"]["board_score_bonus"]["sector"] == 4, "G3b composite=70 → +4")
check(_g3["S4"]["board_score_bonus"]["sector"] == 8, "G3c composite=100 → clamp +8")
check(_g3["S3"]["board_score_bonus"]["sector"] == 0, "G3d composite<50 不扣分（clamp 下界 0）")

# G4 总 cap 80（base 82 + 加成仍封顶 80）
_g4 = mm.derive_board_opportunities(_cats("price_change_24h", "up", [
    _item("C1", sd=8, mcap_tier="top10"),
]), streak_threshold=3)
check(_g4[0]["conviction_score"] == 80, "G4 总 cap 80：base 82 + top10 加成仍 = 80")

# G5 缺字段保守 0（不改基准分）
_g5 = mm.derive_board_opportunities(_cats("price_change_24h", "up", [_item("N1")]), streak_threshold=3)
check(_g5[0]["conviction_score"] == 52
      and _g5[0]["board_score_bonus"] == {"mcap_tier": 0, "vol_mcap": 0, "sector": 0},
      "G5 缺 tier/detail → 无加成，分数=base 52")

# G6 未知 mcap_tier → 0（保守）
_g6 = mm.derive_board_opportunities(_cats("price_change_24h", "up", [
    _item("U1", mcap_tier="top9999")]), streak_threshold=3)
check(_g6[0]["board_score_bonus"]["mcap_tier"] == 0, "G6 未知 mcap_tier → 0")

# G7 常量与 generator 值域一致 + 小批次不分位
check(set(mm._MCAP_TIER_BONUS) == {"top10", "top100", "top500", "top1000"},
      "G7 mcap_tier 键与 daily_diff_generator 值域一致")
check(mm._vol_mcap_bonus_map([{"symbol": "x", "detail": {"vol_mcap_ratio": 1.0}}]) == {},
      "G7b pvs 批次 <4 → 不分位（避免单条被判 top25%）")

# G8 vol_mcap 缺值 / None 不抛异常
_g8 = mm.derive_board_opportunities(_cats("price_volume_surge", "up", [
    _item("W1", detail={}), _item("W2", detail={"vol_mcap_ratio": None}),
    _item("W3", detail={"vol_mcap_ratio": 1.0}), _item("W4", detail={"vol_mcap_ratio": 2.0}),
]), streak_threshold=3)
check(len(_g8) == 4 and all("board_score_bonus" in o for o in _g8),
      "G8 vol_mcap 缺值/None 不抛异常且派生数不变")

# ── H. U-A 连板增强接早报 ──
print("[H] 连板增强接早报（U-A）")
_h_items = [
    {"symbol": "AAA", "streak_days": 5},
    {"symbol": "BBB", "streak_days": 2},                                  # 未达阈值
    {"symbol": "CCC", "streak_days": 4, "streak_start_ambiguous": True},  # 口径变更日 → 剔除
    {"symbol": "DDD", "streak_days": 3},
]
_hits = mm._diff_streak_hits(_h_items)
check([x["symbol"] for x in _hits] == ["AAA", "DDD"],
      "H1 命中集：sd>=3 且非 ambiguous（BBB 2天 / CCC ambiguous 剔除）")
_note = mm._diff_streak_note(_hits, "强势")
check("AAA连续5天" in _note and "持续强势" in _note, "H2 note 含「AAA连续5天」「持续强势」", _note)
check(mm._diff_streak_note([]) == "", "H2b 无命中 → 空 note")
check(mm._diff_streak_bonus(_hits) == 4, "H3 2 命中 → +4（2×2）")
check(mm._diff_streak_bonus([{}] * 5) == 8, "H3b 5 命中 → 封顶 8")

# H4~ 单点集成（真实被三段调用的函数）
_n, _s = mm.augment_diff_streak([{"symbol": "X", "streak_days": 5}], 60, 90, "强势")
check("X连续5天" in _n and _s == 62, "H4 命中 → note 含「X连续5天」、强度 60→62")
_n2, _s2 = mm.augment_diff_streak([{"symbol": "X", "streak_days": 5}], 60, 90, "走弱",
                                  apply_bonus=False)
check("连续5天" in _n2 and _s2 == 60, "H5 连跌只标注不加成（apply_bonus=False）")
_n3, _s3 = mm.augment_diff_streak([{"symbol": "X", "streak_days": 5}], 90, 90, "强势")
check(_s3 == 90, "H6 加成后仍封顶 90")
_n4, _s4 = mm.augment_diff_streak([{"symbol": "X", "streak_days": 2}], 60, 90, "强势")
check(_n4 == "" and _s4 == 60, "H7 sd=2 未达阈值 → 不加注不加成")
_n5, _s5 = mm.augment_diff_streak(
    [{"symbol": "X", "streak_days": 9, "streak_start_ambiguous": True}], 60, 90, "强势")
check(_n5 == "" and _s5 == 60, "H7b ambiguous 连板 → 不加注不加成")

# H8 源码守卫：三段落接线 + 上限值 + 文案拼接
check(_MACRO_SRC.count("augment_diff_streak(") == 4, "H8a 1 定义 + 3 处调用")
check('augment_diff_streak(strong, strength, 90, "强势"' in _MACRO_SRC,
      "H8b price_surge 段接线（cap 90）")
check('augment_diff_streak(crash, strength, 92, "走弱"' in _MACRO_SRC
      and "apply_bonus=False" in _MACRO_SRC, "H8c price_crash 段标注不加成（cap 92）")
check('augment_diff_streak(pvs, strength, 88, "共振"' in _MACRO_SRC,
      "H8d pvs 段接线（cap 88）")
check(mm._DIFF_STREAK_MIN_DAYS == 3, "H8e 阈值常量 3（与 D3 派生一致）")
check("{_note}" in _MACRO_SRC, "H8f note 已拼进 trigger_logic")

# ── 汇总 ──
print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
