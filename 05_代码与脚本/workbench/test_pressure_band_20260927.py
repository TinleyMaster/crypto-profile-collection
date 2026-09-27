#!/usr/bin/env python3
"""抛压分档阈值外置 + 滞回带（A3）离线回归护栏。

背景（2026-09-27）：_compute_pressure_score 的 risk_level 分档此前把 60/30 硬编码在函数内，
违反 market_rules.yaml 头部「P2-4 所有评分/背离阈值全外置」的项目约定；且展示侧 fallback
另有一套 high>=70 / medium>=40（分数尺度也不同）→ 同一字段两套口径。同时实证存在临界抖动：
全表 332 行中 35 行（10.5%）落在 [30, 40) 边界带；PONS 26.96 = ATH 回撤 26.02(96.5%) + OI 0.94，
而 medium 下界 30 分等价 ATH 回撤 -33.3%、该资产当时 -28.91% ⇒ 再跌约 3.4% 即跨档，
实测同日两次计算已在 28.24 → 26.96 漂移。

运行：python workbench/test_pressure_band_20260927.py（纯离线，不连库、不连网）
覆盖：
  1  market_rules.yaml 新增 [unlock_pressure] 段，三键齐备且与改造前裸阈值一致
  2  loader 回退值 = 外置前硬编码值 + band 0（yaml 不可用时行为逐分不变）
  3  裸阈值（无上一档）分档与改造前完全一致
  4  滞回：向上跨档需越过 目标档下界+band；向下跨档需跌破 当前档下界-band；带内维持原档
  5  非法输入（score / previous）安全退化
  6  阈值不再硬编码在 _compute_pressure_score 内；fallback 的 70/40 第二套口径已消除
  7  previous_risk 从 compute_unlock_pressure 一路透传到 _compute_pressure_score
  8  分量为 None（未采集）时标记缺失，与「值为 0」区分；分数口径保持不变
"""
import inspect
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
_YAML_SRC = open(os.path.join(_HERE, "market_rules.yaml"), encoding="utf-8").read()


def _src_of(func_name):
    marker = f"\ndef {func_name}("
    i = _DB_SRC.find(marker)
    if i < 0:
        return ""
    j = _DB_SRC.find("\ndef ", i + len(marker))
    return _DB_SRC[i:j if j > 0 else len(_DB_SRC)]


# ── 1/2 配置外置 ──
print("\n[1/2] market_rules.yaml 外置段与回退默认值")
check("unlock_pressure:" in _YAML_SRC, "yaml 新增 [unlock_pressure] 段")
for k in ("high_threshold:", "medium_threshold:", "hysteresis_band:"):
    check(k in _YAML_SRC, f"yaml 含键 {k}")
check(ds._PRESSURE_BAND_DEFAULTS == {"high_threshold": 60.0, "medium_threshold": 30.0},
      "内建回退 = 外置前硬编码值（60/30）", str(ds._PRESSURE_BAND_DEFAULTS))
check(ds._PRESSURE_RULES["high_threshold"] == 60.0
      and ds._PRESSURE_RULES["medium_threshold"] == 30.0
      and ds._PRESSURE_RULES["hysteresis_band"] == 5.0,
      "运行时已从 yaml 读到 60/30/band=5", str(ds._PRESSURE_RULES))
check("hysteresis_band" in _src_of("_load_unlock_pressure_rules")
      and "0.0" in _src_of("_load_unlock_pressure_rules"),
      "loader 对 band 的默认是 0（= 无滞回 = 改造前行为）")

# ── 3 裸阈值（无上一档）= 改造前行为 ──
print("\n[3] 裸阈值分档（previous=None）")
for score, want in ((0.0, "low"), (29.9, "low"), (30.0, "medium"), (59.9, "medium"),
                    (60.0, "high"), (100.0, "high"), (28.24, "low"), (26.96, "low")):
    check(ds._band_risk_level(score) == want,
          f"score={score} → {want}", f"实际 {ds._band_risk_level(score)}")

# ── 4 滞回 ──
print("\n[4] 滞回带（band=5，med=30，high=60）")
# 向上跨档：low→medium 需 >= 35
check(ds._band_risk_level(34.99, "low") == "low", "low + 34.99 → 维持 low（带内）")
check(ds._band_risk_level(35.0, "low") == "medium", "low + 35.0 → medium（越过下界+band）")
check(ds._band_risk_level(32.0, "low") == "low", "low + 32.0 → 维持 low（正是抖动区）")
# 向下跨档：medium→low 需 <= 25
check(ds._band_risk_level(25.01, "medium") == "medium", "medium + 25.01 → 维持 medium（带内）")
check(ds._band_risk_level(25.0, "medium") == "low", "medium + 25.0 → low（跌破下界-band）")
check(ds._band_risk_level(28.24, "medium") == "medium", "medium + 28.24 → 维持 medium（抖动区）")
# high 边界两侧
check(ds._band_risk_level(65.0, "medium") == "high", "medium + 65.0 → high")
check(ds._band_risk_level(64.99, "medium") == "medium", "medium + 64.99 → 维持 medium")
check(ds._band_risk_level(55.0, "high") == "medium", "high + 55.0 → medium")
check(ds._band_risk_level(55.01, "high") == "high", "high + 55.01 → 维持 high")
# 跨两档（low→high 直跳）需越过 high+band
check(ds._band_risk_level(65.0, "low") == "high", "low + 65.0 → high（直跳够高）")
check(ds._band_risk_level(64.99, "low") == "low", "low + 64.99 → 维持 low（直跳不足带宽）")
# 同档/降档语义（50.0：low→medium 够带宽；medium 维持；high 降到 medium 需 <= 55）
check(ds._band_risk_level(50.0, "low") == "medium", "prev=low + 50.0 → medium")
check(ds._band_risk_level(50.0, "medium") == "medium", "prev=medium + 50.0 → 维持 medium")
check(ds._band_risk_level(50.0, "high") == "medium", "prev=high + 50.0 → medium（50<=55）")
check(ds._band_risk_level(56.0, "high") == "high", "prev=high + 56.0 → 维持 high（带内）")

# ── 5 非法输入 ──
print("\n[5] 非法输入安全退化")
check(ds._band_risk_level(None) == "low", "score=None → low")
check(ds._band_risk_level("abc") == "low", "score 非数 → low")
check(ds._band_risk_level("45") == "medium", "score 数字字符串可转")
for bad_prev in (None, "", "unknown", "LOW", 0):
    check(ds._band_risk_level(32.0, bad_prev) == "medium",
          f"非法 previous={bad_prev!r} → 退化为裸阈值（32 → medium）")

# ── 6 单一口径 ──
print("\n[6] 阈值硬编码已清除（单一口径）")
_cps = _src_of("_compute_pressure_score")
check("_band_risk_level" in _cps, "_compute_pressure_score 走统一分档函数")
check(">= 60" not in _cps and ">= 30" not in _cps,
      "函数体内不再硬编码 60/30 分档")
check("previous_risk" in inspect.signature(ds._compute_pressure_score).parameters,
      "签名含 previous_risk")
_fb = _DB_SRC[_DB_SRC.find("if factors > 0:"):_DB_SRC.find('"fallback_unlock_top10_only"')]
check("_band_risk_level(score)" in _fb, "fallback 走统一分档函数")
check(">= 70" not in _fb and ">= 40" not in _fb,
      "fallback 的 70/40 第二套阈值已消除（该块内）")
check('== "high" if score' not in _DB_SRC, "不存在残留的三元分档写法")
check(ds._compute_pressure_score(0.0, 933.71, 0.0) == (25.0, "low"),
      "既有断言口径不变（25.0/low）")
check(ds._compute_pressure_score(0.0, 50.0, 0.0) == (12.5, "low"),
      "既有断言口径不变（12.5/low）")

# ── 7 previous_risk 透传 ──
print("\n[7] previous_risk 透传（端到端）")
_cup = _src_of("compute_unlock_pressure")
check("previous_risk = row[\"risk_level\"] if row else None" in _cup,
      "compute_unlock_pressure 取库中上一档作滞回基准")
check("previous_risk=previous_risk" in _cup, "调用 _compute_pressure_score 时透传")
# 无上一档 → 31.2 判 medium；有上一档 low → 未越过 35，维持 low（滞回生效）
check(ds._compute_pressure_score(5.2, 0.0, 0.0) == (31.2, "medium"),
      "无上一档：31.2 → medium")
check(ds._compute_pressure_score(5.2, 0.0, 0.0, previous_risk="low") == (31.2, "low"),
      "有上一档 low：31.2 → 维持 low（滞回抑制抖动）")
check(ds._compute_pressure_score(5.2, 0.0, 0.0, previous_risk="medium") == (31.2, "medium"),
      "有上一档 medium：31.2 → 维持 medium")
# 滞回基准取自「过期但已展示」的行：源码上缓存早退分支不影响 previous_risk 取值
check(_cup.find("previous_risk = row[") < _cup.find("score, risk = _compute_pressure_score("),
      "滞回基准在算分之前确定（含 force 路径）")

# ── 8 分量缺失标记（A3 相邻缺陷：缺失当零）──
print("\n[8] 缺失分量标记（缺失 ≠ 零风险）")
check(ds._PRESSURE_COMPONENT_TOTAL == 6, "分量总数常量为 6（unlock/concentration/drawdown/cvd/oi/unlock_value）")
check(ds._pressure_missing_inputs(80.0, 0.1, -30.0, -0.1, -5.0) == [],
      "输入齐备 → 无缺失")
check(ds._pressure_missing_inputs(None, 0.1, -30.0, -0.1, -5.0) == ["concentration"],
      "top10=None → 标记 concentration")
check(ds._pressure_missing_inputs(None, None, None, None, None)
      == ["concentration", "turnover", "drawdown", "cvd", "oi"],
      "全缺 → 五个分量 key 全标")
check(ds._pressure_missing_inputs(0.0, 0.0, 0.0, 0.0, 0.0) == [],
      "值为 0（已查明）不被误标为缺失（关键区分）")
# 分数口径不变：None 仍按 0 计分（口径变更留给产品拍板，本轮只标记）
check(ds._compute_pressure_score(0.0, None, 0.0) == (0.0, "low"),
      "top10 缺失：分数口径不变（0.0/low，未擅自抬分）")
check(ds._compute_pressure_score(0.0, 80.0, 0.0) == (20.0, "low"),
      "top10=80：集中度分量 20 分（= 缺失时被低估的量级，可跨 30 分档）")
# 写路径接线
_cup2 = _src_of("compute_unlock_pressure")
check("missing_inputs = _pressure_missing_inputs(" in _cup2,
      "写路径调用 _pressure_missing_inputs")
check('"missing_inputs": missing_inputs' in _cup2 and '"is_partial": bool(missing_inputs)' in _cup2,
      "写路径 detail 与顶层返回值含 missing_inputs / is_partial")
check("_PRESSURE_COMPONENT_TOTAL - len(missing_inputs)" in _cup2,
      "components_available 由缺失数推导")
_cached_blk = _cup2[_cup2.find("if row and not force:"):_cup2.find("previous_risk = row[")]
check("missing_inputs" in _cached_blk and "is_partial" in _cached_blk,
      "缓存早退分支同样透出缺失标记（旧行无键 → 回退为齐备）")
# 展示 / 结论侧透出
check('for _mk in ("missing_inputs", "components_available", "components_total",' in _DB_SRC,
      "notebook 展示侧循环透出四个缺失标记字段")
check('result["pressure"][_mk] = _p[_mk]' in _DB_SRC,
      "notebook 展示侧写入 pressure 块")
check('metrics_structured["pressure"]["is_partial"]' in _DB_SRC,
      "结构化指标注入 is_partial（供 LLM 消费）")
check('pressure.is_partial = true' in _DB_SRC,
      "system_prompt 规则 5 增加缺失子条（禁止把缺失低分断言为「无抛压」）")

print("\n" + "=" * 60)
print(f"通过 {passed} / 失败 {failed}")
print("=" * 60)
sys.exit(1 if failed else 0)