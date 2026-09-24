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

# ── 汇总 ──
print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
