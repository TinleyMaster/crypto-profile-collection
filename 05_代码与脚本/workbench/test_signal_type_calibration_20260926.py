#!/usr/bin/env python3
"""刀2（2026-09-26 审计·P0-B）「回测 → 权重」反馈闭环 · 离线回归护栏。

来源：audit_高亮信号_确定性审计与优化_2026-09-26.md §六 刀2 + OBI-OPT-BACKTEST-001 三阻塞。
运行：python workbench/test_signal_type_calibration_20260926.py（纯离线，不连库、不连网）

覆盖：
  回测侧  D3  token_unlock 持有期去掉 h=0（原 entry==exit → pnl 恒 0 → hit_rate 假 0%）
  回测侧  D2  NOT_BACKTESTABLE 覆盖全部聚合/非可交易 target 类型；EXEMPT = NOT_CALIBRABLE ∪ NOT_BACKTESTABLE
  回测侧  D1  批量取价 SQL 口径（DISTINCT ON 同 symbol 取 asset_id 最小 + 排除包装/桥接币）
  回测侧  门控  _gate_for 六种分支（豁免×2 / preliminary / calibrated_low / calibrated_ok）
  回测侧  落表  ON CONFLICT 键 = (signal_type, horizon_days, window_end)，skipped 计数入库
  macro 侧 无校准 → 行为与上线前一致（HIGH 保留）
  macro 侧  calibrated_low/preliminary → 分数×0.6、档位封顶 MED、卡片保留（不判 LOW）
  macro 侧  保卡下限：衰减后仍不低于 med_min（防整类被删）
  macro 侧  豁免（exempt_*）→ 不衰减，但**默认封顶 MED**（C1：未回测 = 无背书，白名单内除外）
  macro 侧  豁免白名单 exempt_allow_high（yaml 真源，当前空）→ 列入才允许进 HIGH
  macro 侧  消费 SQL 取最新窗口（window_end DESC）+ no_high/weight_factor 字段
  macro 侧  复验 47bcb4d §五#1  calibration_status 四态留痕（missing/calibrated_ok/decayed/exempt_*）
  macro 侧  复验 47bcb4d §五#4  raw_before_decay 供 select_highlight_signals 排序还原原序
  macro 侧  复验 47bcb4d §五#1  前端按状态渲染「已校准 / 未校准」角标
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import macro_market as mm            # noqa: E402
import backtest_opportunities as bt  # noqa: E402

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
_BT_SRC = open(os.path.join(_HERE, "backtest_opportunities.py"), encoding="utf-8").read()

_T = dict(mm.OPPORTUNITY_THRESHOLDS_DEFAULT)


def _push(score_raw, st, tgt="BTC", cal=None, t=None):
    """钉死校准条目后跑 _push_opportunity，返回 (opp, excluded, opportunities)。"""
    mm._SIGNAL_TYPE_CALIBRATION.clear()
    if cal is not None:
        mm._SIGNAL_TYPE_CALIBRATION[st] = cal
    mm._CALIB_LOADED_AT = float("inf")   # 永不触发惰性加载 ⇒ 不连库
    opp = {"signal_type": st, "target": tgt, "conviction_score": score_raw}
    exc, opps = [], []
    mm._push_opportunity(opp, opps, exc, t if t else _T, cycle_phase="mid", n_confirm=1)
    return opp, exc, opps


_CAL_LOW = {"sample_count": 44, "hit_rate": 0.4091, "weight_factor": 0.6,
            "gate": "calibrated_low", "no_high": True, "window_end": "2026-09-25"}
_CAL_OK = {"sample_count": 45, "hit_rate": 0.7778, "weight_factor": 1.0,
           "gate": "calibrated_ok", "no_high": False, "window_end": "2026-09-25"}
_CAL_EXEMPT = {"sample_count": 0, "hit_rate": None, "weight_factor": 1.0,
               "gate": "exempt_not_backtestable", "no_high": False, "window_end": "2026-09-25"}


# ═══════════════ 回测侧：D3 token_unlock h=0 退化 ═══════════════
print("[D3] token_unlock 持有期不得含 h=0（entry==exit → pnl 恒 0）")
check(0 not in bt.SIGNAL_HORIZONS["token_unlock"],
      f"token_unlock horizons 无 0（实得 {bt.SIGNAL_HORIZONS['token_unlock']}）")
check(bt.SIGNAL_HORIZONS["token_unlock"] == [1, 3, 7],
      f"token_unlock = [1, 3, 7]（实得 {bt.SIGNAL_HORIZONS['token_unlock']}）")
check(all(0 not in v for v in bt.SIGNAL_HORIZONS.values()),
      "所有 signal_type 的持有期均无 0（防同型退化复发）")


# ═══════════════ 回测侧：D2 不可回测集合 ═══════════════
print("[D2] NOT_BACKTESTABLE 覆盖聚合/非可交易 target 类型 + 豁免集合定义")
for st in ("funding", "mvrv_under_watch", "mvrv_deep_over", "mvrv_over_watch",
           "narrative", "chain_inflow", "sector_inflow", "sector_outflow",
           "fng_extreme", "leverage_extreme", "stablecoin_inflow"):
    check(st in bt.NOT_BACKTESTABLE, f"{st} 已登记为不可回测（豁免）")
check(bt.NOT_CALIBRABLE == {"mvrv_deep_under"}, "NOT_CALIBRABLE = {mvrv_deep_under}")
check(bt.EXEMPT_SIGNAL_TYPES == bt.NOT_CALIBRABLE | bt.NOT_BACKTESTABLE,
      "EXEMPT_SIGNAL_TYPES = NOT_CALIBRABLE ∪ NOT_BACKTESTABLE")
check(not (bt.NOT_CALIBRABLE & bt.NOT_BACKTESTABLE), "两个豁免集合无交集（否则 gate 归属歧义）")

print("[D2] target 形状判定（聚合类不可取价）")
check(bt._is_symbol_target("BTC") and bt._is_symbol_target("brb")
      and bt._is_symbol_target("2Z"), "纯 symbol target 判定为 True")
check(not bt._is_symbol_target("Base 链") and not bt._is_symbol_target("2 币 MVRV 极度高估")
      and not bt._is_symbol_target("恐贪指数极度贪婪") and not bt._is_symbol_target("AI & Big Data"),
      "聚合/指数类 target 判定为 False")


# ═══════════════ 回测侧：D1 批量取价口径 ═══════════════
print("[D1] 批量取价 SQL 口径")
check("DISTINCT ON (UPPER(a.canonical_symbol), m.market_date)" in _BT_SRC
      and "ORDER BY UPPER(a.canonical_symbol), m.market_date, a.asset_id" in _BT_SRC,
      "同 symbol 多 asset 时取 asset_id 最小（与原 ORDER BY asset_id LIMIT 1 同口径）")
check("NOT LIKE '%%Wrapped%%'" in _BT_SRC and "NOT LIKE '%%Bridged%%'" in _BT_SRC,
      "排除包装/桥接币（腐败价源）")
check("_fetch_opportunities" in _BT_SRC and "jsonb_array_elements" in _BT_SRC,
      "机会抽取走 SQL jsonb（不再逐快照拉整份 payload）")
check("_BTC_CACHE" in _BT_SRC and "_EXTERNAL_CACHE" in _BT_SRC and "MAX_EXTERNAL_LOOKUPS" in _BT_SRC,
      "BTC 基准/外部取价有缓存与外呼上限（防回测跑不完）")


# ═══════════════ 回测侧：门控纯函数 ═══════════════
print("[门控] _gate_for 六分支")
check(bt._gate_for("mvrv_deep_under", 0, None) == ("exempt_not_calibrable", 1.0, True),
      "NOT_CALIBRABLE → 豁免不衰减，但默认封顶 MED（C1：未回测无背书）")
check(bt._gate_for("narrative", 0, None) == ("exempt_not_backtestable", 1.0, True),
      "NOT_BACKTESTABLE → 豁免不衰减，但默认封顶 MED（C1）")
check(bt._gate_for("github_activity", 28, 0.6071) == ("preliminary", 0.6, True),
      "样本 28 < 30 → preliminary，×0.6 且不进 HIGH（即便命中率 60.7%）")
check(bt._gate_for("etf_flow", 44, 0.4091) == ("calibrated_low", 0.6, True),
      "样本 44、命中率 40.9% < 50% → calibrated_low，×0.6 且不进 HIGH")
check(bt._gate_for("catalyst", 45, 0.7778) == ("calibrated_ok", 1.0, False),
      "样本 45、命中率 77.8% → calibrated_ok，恒等（与不校准等价）")
check(bt._gate_for("whale_flow", 67, 0.4776) == ("calibrated_low", 0.6, True),
      "whale_flow 47.8%（修正口径后跌破 50%）→ calibrated_low")
check(bt.DECAY_FACTOR == 0.60 and bt.MIN_SAMPLES == 30 and bt.HIT_RATE_MIN == 0.5,
      "阈值常量与 macro 消费口径一致（0.60 / 30 / 0.5）")

print("[落表] 幂等键与 skipped 计数入库")
check("ON CONFLICT (signal_type, horizon_days, window_end) DO UPDATE" in _BT_SRC,
      "upsert 键 = (signal_type, horizon_days, window_end)")
for col in ("skipped_dup", "skipped_no_price", "skipped_not_backtestable",
            "weight_factor", "gate", "no_high"):
    check(col in _BT_SRC, f"落表含 {col}")
check('"skipped"' in _BT_SRC and "no_signal_type" in _BT_SRC,
      "跳过计数进入返回值（原实现算完即丢）")
check("persist" in _BT_SRC and '"calibration_rows_written"' in _BT_SRC,
      "--write 落表 + 返回写入行数")

# 表结构保真：迁移 SQL 与写入列一致
_MSQL = open(os.path.join(_HERE, "..", "scripts", "migrations",
                          "fix_070_signal_type_calibration.sql"), encoding="utf-8").read()
for col in ("signal_type", "horizon_days", "sample_count", "hit_rate", "avg_alpha",
            "skipped_dup", "skipped_no_price", "skipped_not_backtestable",
            "weight_factor", "gate", "no_high", "window_start", "window_end"):
    check(col in _MSQL, f"迁移 DDL 含列 {col}")
check("UNIQUE (signal_type, horizon_days, window_end)" in _MSQL,
      "迁移 DDL 唯一键与 ON CONFLICT 一致")


# ═══════════════ macro 侧：无校准 = 与上线前一致 ═══════════════
print("[macro] 无校准 → 行为与上线前完全一致")
hi, hi_exc, hi_opps = _push(95, "etf_flow")          # etf_flow 无校准条目
check(hi["conviction_tier"] == "HIGH" and hi["confidence"] == "high",
      f"无校准 → raw 95 仍 HIGH/high（实得 {hi['conviction_tier']}）")
check("calibration" not in hi and "calibration_note" not in hi and "tier_demote_reason" not in hi,
      "无校准 → 不写 calibration 字段（不污染卡片）")

check(mm._signal_type_calibration("nonexistent_type") is None, "未登记类型返回 None")
mm._SIGNAL_TYPE_CALIBRATION.clear()
mm._SIGNAL_TYPE_CALIBRATION["catalyst"] = _CAL_OK
mm._CALIB_LOADED_AT = float("inf")
check(mm._signal_type_calibration("catalyst") is None,
      "calibrated_ok（factor=1 且 no_high=False）→ None（恒等，无需标注）")


# ═══════════════ macro 侧：衰减 + 封顶 MED + 保卡 ═══════════════
print("[macro] calibrated_low → 分数×0.6 + 档位封顶 MED + 卡片保留")
_med = int(_T.get("conviction_med_min", 55))
# 86×0.6=52 低于 med_min，按「保卡下限」语义实得 med_min（不因衰减被删卡）
low, low_exc, low_opps = _push(86, "etf_flow", cal=_CAL_LOW)
check(low["conviction_score"] == max(round(86 * 0.6), _med),
      f"分数 86 → max(×0.6, {_med})（实得 {low['conviction_score']}）")
check(low["conviction_tier"] == "MED" and low["confidence"] == "medium",
      f"档位封顶 MED（实得 {low['conviction_tier']}）")
check(low in low_opps and low not in low_exc, "卡片保留在 opportunities（不因衰减被删）")
check(low.get("calibration", {}).get("gate") == "calibrated_low"
      and low.get("calibration", {}).get("score_before_decay") == 86,
      "落 calibration 溯源（gate + 衰减前分数）")
check("不进 HIGH 候选" in (low.get("tier_demote_reason") or "")
      and "41%" in (low.get("calibration_note") or ""),
      f"降档原因可读（实得 {low.get('tier_demote_reason')!r}）")

print("[macro] 衰减不触底时严格 = ×0.6（保卡下限不干扰）")
low0, low0_exc, low0_opps = _push(100, "etf_flow", cal=_CAL_LOW)  # 100*0.6=60 > 55
check(low0["conviction_score"] == int(round(100 * 0.6)) and low0["conviction_tier"] == "MED",
      f"100 → {int(round(100*0.6))}/MED（实得 {low0['conviction_score']}/{low0['conviction_tier']}）")

print("[macro] 保卡下限：衰减后跌破 med_min 时 floor 到 med_min，不判 LOW")
low2, low2_exc, low2_opps = _push(70, "etf_flow", cal=_CAL_LOW)   # 70*0.6=42 < 55
check(low2["conviction_score"] == _med,
      f"70×0.6=42 被 floor 到 med_min={_med}（实得 {low2['conviction_score']}）")
check(low2["conviction_tier"] == "MED" and low2 in low2_opps,
      "档位 MED 且卡片保留（封顶而非封杀）")

print("[macro] preliminary（样本不足）同样衰减 + 封顶")
pre = {"sample_count": 5, "hit_rate": 0.2, "weight_factor": 0.6,
       "gate": "preliminary", "no_high": True, "window_end": "2026-09-25"}
p, p_exc, p_opps = _push(92, "kol_onchain", cal=pre)
check(p["conviction_score"] == int(round(92 * 0.6)) and p["conviction_tier"] == "MED",
      f"92 → {int(round(92*0.6))}/MED（实得 {p['conviction_score']}/{p['conviction_tier']}）")

print("[macro] 豁免类型 → 不衰减，但默认封顶 MED（C1）")
ex, ex_exc, ex_opps = _push(88, "narrative", tgt="AI & Big Data", cal=_CAL_EXEMPT)
check(ex["conviction_score"] == 88 and ex["conviction_tier"] == "MED",
      f"豁免 → 分数 88 原样保留（不衰减），档位封顶 MED（实得 {ex['conviction_score']}/{ex['conviction_tier']}）")
check("calibration" not in ex, "豁免不写 calibration 字段（不制造「已校准」错觉）")
check("exempt_unbacktested" in (ex.get("tier_demote_reason") or ""),
      f"降档原因可解释（实得 {ex.get('tier_demote_reason')}）")


# ═══════════════ macro 侧：复验 47bcb4d §五 #1 四态状态留痕 ═══════════════
# 复验发现「calibrated_ok（有回测背书）」与「表中无此类型/豁免未回测」在 API 上同形，
# HIGH 卡无法自证高确定性是否有经验支撑。_calibration_status 另记一层状态以区分。
print("[复验#1] calibration_status 四态：missing / calibrated_ok / decayed / exempt_*")
ms, _, _ = _push(95, "etf_flow")                      # 表内无此类型
check(ms["calibration_status"]["state"] == "missing"
      and ms["calibration_status"]["calibrated"] is False
      and ms["calibration_status"]["gate"] == "missing_calibration",
      f"表内无此类型 → missing（实得 {ms['calibration_status']['state']}）")
ok, _, _ = _push(95, "catalyst", cal=_CAL_OK)
check(ok["calibration_status"]["state"] == "calibrated_ok"
      and ok["calibration_status"]["calibrated"] is True
      and ok["calibration_status"]["sample_count"] == 45,
      f"已回测且背书通过 → calibrated_ok（实得 {ok['calibration_status']['state']}）")
check("calibration" not in ok,
      "calibrated_ok 不写重 calibration 字段（状态另挂，不污染卡片）")
check(ok["conviction_score"] == 95 and ok["conviction_tier"] == "HIGH",
      "calibrated_ok → 不衰减、保留 HIGH（与上线前一致）")
check(ok["calibration_status"]["state"] != ms["calibration_status"]["state"],
      "有背书 / 未校准 在 API 上可分辨（复验#1 核心验收点）")
dc, _, _ = _push(86, "etf_flow", cal=_CAL_LOW)
check(dc["calibration_status"]["state"] == "decayed"
      and dc["calibration_status"]["calibrated"] is True,
      f"已回测但降权 → decayed（实得 {dc['calibration_status']['state']}）")
check(ex["calibration_status"]["state"] == "exempt_not_backtestable"
      and ex["calibration_status"]["calibrated"] is False,
      f"豁免类型 → exempt_not_backtestable 且无背书（实得 {ex['calibration_status']['state']}）")
# 状态层是**另记**的：_signal_type_calibration 的既有契约（None 语义）不变，
# 故 calibrated_ok / missing 仍不产出 calibration 字段（见上方 ok / ms 两例）。
check(ms.get("raw_before_decay") is None and ok.get("raw_before_decay") is None,
      "未衰减卡不写 raw_before_decay（无衰减即无原序信息）")

# ═══════════════ macro 侧：复验 47bcb4d §五 #4 保卡下限排序还原 ═══════════════
print("[复验#4] raw_before_decay 供排序：MED 内还原原始质量序")
check(dc.get("raw_before_decay") == 86 and ok.get("raw_before_decay") is None,
      f"衰减卡记 raw_before_decay，未衰减卡不记（实得 {dc.get('raw_before_decay')}）")


def _mk(target, score, raw=None, st="etf_flow"):
    o = {"signal_type": st, "target": target, "direction": "long",
         "conviction_score": score, "conviction_tier": "MED",
         "related_dims": ["a", "b"]}
    if raw is not None:
        o["raw_before_decay"] = raw
    return o


# 同分（55，均为保卡下限落点）时按衰减前原分排序：raw 78 应排在 raw 58 之前
_ordered = mm.select_highlight_signals([_mk("BBB", 55, raw=58), _mk("AAA", 55, raw=78)],
                                       max_total=10, min_resonance=1)
check([x["target"] for x in _ordered] == ["AAA", "BBB"],
      f"同分按 raw_before_decay 降序（实得 {[x['target'] for x in _ordered]}）")
# 无校准字段的卡：末位 tie-break 退化为自身分数 ⇒ 与改动前一致（同分保持输入序）
_plain = mm.select_highlight_signals([_mk("BBB", 55), _mk("AAA", 55)],
                                     max_total=10, min_resonance=1)
check([x["target"] for x in _plain] == ["BBB", "AAA"],
      f"无 raw_before_decay → 排序行为不变（实得 {[x['target'] for x in _plain]}）")
# 主序仍是分数：分数高的 raw 低的卡不被反超
_main = mm.select_highlight_signals([_mk("BBB", 60, raw=10), _mk("AAA", 55, raw=99)],
                                    max_total=10, min_resonance=1)
check([x["target"] for x in _main] == ["BBB", "AAA"],
      f"conviction_score 仍是主序（实得 {[x['target'] for x in _main]}）")


# ═══════════════ macro 侧：源码守卫 ═══════════════
print("[macro] 消费端源码守卫")
check("SELECT DISTINCT ON (signal_type)" in _MACRO_SRC
      and "ORDER BY signal_type, window_end DESC" in _MACRO_SRC,
      "取每类型最新窗口（window_end DESC）")
check("_CALIB_TTL_SEC" in _MACRO_SRC and "_ensure_calibration_loaded" in _MACRO_SRC,
      "惰性加载 + TTL（不在 import 期打 DB）")
check("logger.warning" in _MACRO_SRC.split("def _load_signal_type_calibration(")[1].split("\ndef ")[0],
      "加载失败记 warning 后降级为 {}（不阻断主流程）")
_push_body = _MACRO_SRC.split("def _push_opportunity(")[1].split("\ndef ")[0]
check("_signal_type_calibration(st)" in _push_body
      and "score * cal[\"weight_factor\"]" in _push_body.replace("'", '"')
      and "max(int(round(" in _push_body,
      "衰减写在 _push_opportunity 内且带 med_min 保卡下限")
check('tier = "MED"' in _push_body and "no_high" in _push_body,
      "no_high → 档位封顶 MED 落在同一函数（单一落点）")
check("_calibration_status(st)" in _push_body and 'opp["calibration_status"]' in _push_body,
      "复验#1：状态留痕写在 _push_opportunity 内（每条机会都有）")
check('opp["raw_before_decay"] = before' in _push_body,
      "复验#4：衰减前原分落在 _push_opportunity 内")
_sort_body = _MACRO_SRC.split("def select_highlight_signals(")[1].split("\ndef ")[0]
check("raw_before_decay" in _sort_body and "raw = score" in _sort_body,
      "复验#4：排序键消费 raw_before_decay，且缺省退化为自身分数")

# 前端：状态角标渲染
_HTML_SRC = open(os.path.join(_HERE, "templates", "index.html"), encoding="utf-8").read()
check("o.calibration_status" in _HTML_SRC and "signal-calib-none" in _HTML_SRC
      and "signal-calib-ok" in _HTML_SRC,
      "复验#1：前端按 calibration_status 渲染「已校准/未校准」角标（未校准灰标）")

# ═══════════════ C1/C2（FIX-DETERMINACY-002，2026-09-27）：豁免封顶 + HIGH 成色可见 ═══════════════
print("[C1] 豁免类默认封顶 MED + yaml 白名单 exempt_allow_high")
check(bt.EXEMPT_ALLOW_HIGH == set(), f"白名单当前为空（实得 {bt.EXEMPT_ALLOW_HIGH}）")
_old_allow = bt.EXEMPT_ALLOW_HIGH
try:
    bt.EXEMPT_ALLOW_HIGH = {"narrative"}
    check(bt._gate_for("narrative", 0, None) == ("exempt_not_backtestable", 1.0, False),
          "白名单内豁免类型 → 不封顶（可进 HIGH）")
finally:
    bt.EXEMPT_ALLOW_HIGH = _old_allow
_YAML = open(os.path.join(_HERE, "market_rules.yaml"), encoding="utf-8").read()
check("exempt_allow_high:" in _YAML, "market_rules.yaml 登记白名单键（真源）")
check("exempt_allow_high" in mm.OPPORTUNITY_THRESHOLDS_DEFAULT,
      "默认阈值表登记该键（否则 yaml 覆盖因 key 不在 target 而失效）")
check("_exempt_no_high" in _MACRO_SRC and "_EXEMPT_ALLOW_HIGH" in _MACRO_SRC,
      "macro 即时封顶（不等周级回测刷新表）")
# 钉死豁免判定（含最高分 mvrv_deep_over 91，13 条 HIGH 中 8 条属此类）
mm._SIGNAL_TYPE_CALIBRATION.clear()
mm._SIGNAL_TYPE_CALIBRATION["narrative"] = _CAL_EXEMPT
mm._SIGNAL_TYPE_CALIBRATION["mvrv_deep_over"] = dict(_CAL_EXEMPT)
mm._SIGNAL_TYPE_CALIBRATION["catalyst"] = _CAL_OK
mm._CALIB_LOADED_AT = float("inf")
check(mm._exempt_no_high("narrative") and mm._exempt_no_high("mvrv_deep_over"),
      "豁免类型 → 应封顶 MED（含 mvrv_deep_over）")
check(not mm._exempt_no_high("catalyst"), "有背书的 calibrated_ok 不封顶（保留 HIGH）")
try:
    mm._EXEMPT_ALLOW_HIGH.add("narrative")
    ex2, _, _ = _push(88, "narrative", tgt="AI & Big Data", cal=_CAL_EXEMPT)
    check(ex2["conviction_tier"] == "HIGH",
          f"白名单命中 → HIGH 保留（实得 {ex2['conviction_tier']}）")
finally:
    mm._EXEMPT_ALLOW_HIGH.discard("narrative")
mm._SIGNAL_TYPE_CALIBRATION.clear()

print("[C2] 高亮区顶部 HIGH 成色指标")
check("signal-high-credibility" in _HTML_SRC and "已校准背书" in _HTML_SRC
      and "未回测" in _HTML_SRC and "_calibOk" in _HTML_SRC,
      "前端渲染「HIGH N：已校准背书 X / 未回测 Y」")

print(f"\n{'=' * 60}\n通过 {passed} / 失败 {failed}")
sys.exit(1 if failed else 0)