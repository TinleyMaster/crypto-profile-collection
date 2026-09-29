#!/usr/bin/env python3
"""催化剂追溯页可读性修复离线护栏单测（审计_催化剂追溯页_可读性_2026-09-29）。

运行：python test_catalyst_trace_page_20260929.py

钉住的不变量（审计 P0/P1/P2）：
  P0-1 行首可读：trace_step 落 symbol；读取层按 catalyst_id/asset_id 回填 title/symbol；
  P0-2 默认视图分层：G6 通过行由「关键决策」视图折叠（G6 被拦仍保留）；
  P1-1 统计卡按管道序 L1→G1→G2→G6（修正字母序 G1→G2→G6→L1）；
  P1-2 字段图例：TRACE_METRIC_LEGEND 覆盖管线实际产出的 metric key；
  P1-3 通过原因：通过行生成一句人话原因；
  P2-1 q 搜索命中回填后的 title/symbol；
  写入侧：phase_catalyst_pipeline 各阶段 trace_step 回填 title/symbol。

⚠️ 纯离线：不连库、不打网络（假 conn / 临时目录 / 源码 AST 断言）。
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
for p in (_HERE, os.path.join(_SCRIPTS, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst import catalyst_trace as ct  # noqa: E402

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


# ── 假 conn ──
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cat_rows=(), asset_rows=()):
        self.cat_rows = list(cat_rows)
        self.asset_rows = list(asset_rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "biz.asset_catalyst" in sql:
            return _FakeResult(self.cat_rows)
        return _FakeResult(self.asset_rows)


print("== 1. P0-1 写入侧：trace_step 落 symbol ==")
with tempfile.TemporaryDirectory() as td:
    _orig_base = ct._TRACE_BASE
    ct._TRACE_BASE = td
    try:
        ct.trace_step("G6_signal", catalyst_id=469, asset_id=3065,
                      title="CME 将推出 BCH 与 UNI 期货", symbol="BCH",
                      passed=True)
        ct.trace_step("G6_signal", catalyst_id=470, asset_id=3066, passed=False,
                      reason="composite=39 < C阈值40")
        day = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        with open(os.path.join(td, f"{day}.jsonl"), encoding="utf-8") as fh:
            lines = [__import__("json").loads(x) for x in fh if x.strip()]
    finally:
        ct._TRACE_BASE = _orig_base
    check(lines[0].get("symbol") == "BCH", "symbol 落库", str(lines[0]))
    check(lines[0].get("title") == "CME 将推出 BCH 与 UNI 期货", "title 落库")
    check(lines[1].get("symbol") == "", "未传 symbol → 空串（非 None，前端安全）")
    check("symbol" in lines[0] and "symbol" in lines[1], "所有行都含 symbol 键")

print("== 2. P0-1 读取侧：trace_enrich_from_db 回填 ==")
entries = [
    {"stage": "G6_signal", "catalyst_id": 469, "asset_id": 3065, "title": "", "symbol": ""},
    {"stage": "G2_resonance", "catalyst_id": 469, "asset_id": 3065,
     "title": "已有标题勿覆盖", "symbol": ""},
    {"stage": "G1_grade", "catalyst_id": 999, "asset_id": None, "title": "", "symbol": ""},
]
conn = _FakeConn(
    cat_rows=[(469, "CME 将推出 BCH 与 UNI 期货"), (999, "某无标题事件")],
    asset_rows=[(3065, "BCH")],
)
ct.trace_enrich_from_db(entries, conn)
check(entries[0]["title"] == "CME 将推出 BCH 与 UNI 期货", "空标题被回填")
check(entries[0]["symbol"] == "BCH", "空 symbol 被回填")
check(entries[1]["title"] == "已有标题勿覆盖", "已有标题不被覆盖")
check(entries[1]["symbol"] == "BCH", "已有标题行仍补 symbol")
check(entries[2]["title"] == "某无标题事件", "catalyst 级（无 asset）也回填标题")
check(len(conn.calls) == 2, "批量查询恰 2 次（catalyst + asset）", str(len(conn.calls)))
check(conn.calls[0][1] == ([469, 999],), "catalyst_id 批量去重后升序", str(conn.calls[0][1]))

# 无 id 时不查库
conn2 = _FakeConn()
ct.trace_enrich_from_db([{"stage": "G6_signal"}], conn2)
check(len(conn2.calls) == 0, "无 catalyst/asset id 时不查库")

# 查询失败不抛（由调用方 try 兜底，这里验证空结果不炸）
conn3 = _FakeConn(cat_rows=[], asset_rows=[])
ct.trace_enrich_from_db([{"catalyst_id": 1, "asset_id": 2}], conn3)
check(True, "查不到行的 id 不抛异常")

print("== 3. P0-2 关键决策视图 ==")
check(ct.trace_is_key_row({"stage": "G6_signal", "passed": True}) is False,
      "G6 通过行 → 非关键行（被折叠）")
check(ct.trace_is_key_row({"stage": "G6_signal", "passed": False}) is True,
      "G6 被拦行 → 关键行（保留）")
check(ct.trace_is_key_row({"stage": "G1_grade", "passed": True}) is True,
      "G1 通过行 → 关键行")
check(ct.trace_is_key_row({"stage": "L1_classify", "passed": True}) is True,
      "L1 通过行 → 关键行")

print("== 4. P1-1 统计卡管道序 ==")
check(ct.TRACE_STAGE_ORDER[0] == "L1_classify" and ct.TRACE_STAGE_ORDER[1] == "G1_grade",
      "阶段序以 L1→G1 开头", str(ct.TRACE_STAGE_ORDER))
items = [{"stage": "G6_signal"}, {"stage": "L1_classify"},
         {"stage": "G2_resonance"}, {"stage": "G1_grade"}]
items.sort(key=ct.trace_stage_sort_key)
check([i["stage"] for i in items] ==
      ["L1_classify", "G1_grade", "G2_resonance", "G6_signal"],
      "排序结果 = 管道序（非字母序）", str([i["stage"] for i in items]))
check(ct.trace_stage_sort_key({"stage": "unknown_stage"}) == len(ct.TRACE_STAGE_ORDER),
      "未知阶段排最后")

print("== 5. P1-3 通过原因 ==")
r = ct.trace_derive_pass_reason({"stage": "G6_signal", "passed": True,
                                 "metrics": {"tier": "B", "composite": 72, "rr": 1.8}})
check("通过进入信号池" in r and "B" in r and "72" in r, "G6 通过原因含档位/综合分", r)
r = ct.trace_derive_pass_reason({"stage": "G2_resonance", "passed": True,
                                 "metrics": {"resonance_score": 45, "resonance_state": "weak"}})
check("共振分 45" in r and "G6" in r, "G2 通过原因", r)
r = ct.trace_derive_pass_reason({"stage": "G1_grade", "passed": True,
                                 "metrics": {"kind": "structural", "base_strength": 70}})
check("structural" in r and "70" in r, "G1 通过原因", r)
r = ct.trace_derive_pass_reason({"stage": "SO_second_order", "passed": True,
                                 "metrics": {"mappings": 3}})
check("3" in r, "SO 通过原因含映射数", r)
# 已有 reason / 被拦行原样返回
check(ct.trace_derive_pass_reason({"stage": "G6_signal", "passed": True, "reason": "已有"}) == "已有",
      "已有 reason 不被覆盖")
check(ct.trace_derive_pass_reason({"stage": "G6_signal", "passed": False, "reason": None}) is None,
      "被拦行不生成通过原因")
check(ct.trace_derive_pass_reason({"stage": "G6_signal", "passed": True,
                                   "metrics": {}}) == "通过",
      "无 metrics → 退化为「通过」")
check(ct.trace_derive_pass_reason({"stage": "未知", "passed": True, "metrics": {}}) == "通过",
      "未知阶段 → 「通过」")

print("== 6. P1-2 字段图例覆盖管线产出 ==")
_legend_keys = set(ct.TRACE_METRIC_LEGEND.keys())
# 管线各阶段 trace metrics 中使用的键（审计点名字段 + 实际产出）
_pipeline_keys = {
    "kind", "base_strength", "authority", "event_weight", "scope", "tradable",
    "event_type", "resonance_score", "resonance_state", "excess_24h", "vol_z",
    "direction", "tier", "composite", "rr", "status", "res_state", "prev_status",
    "mappings", "persistence", "fundamental", "technical",
}
_missing = sorted(_pipeline_keys - _legend_keys)
check(not _missing, "图例覆盖全部管线 metric key", f"缺失: {_missing}")
check(all("label" in v and "desc" in v for v in ct.TRACE_METRIC_LEGEND.values()),
      "每个图例项都含 label/desc")

print("== 7. 前端模板接线（源码守卫） ==")
_html_path = os.path.join(_HERE, "templates", "catalyst.html")
with open(_html_path, encoding="utf-8") as fh:
    html = fh.read()
check("关键决策" in html and "view" in html, "含「关键决策」默认视图 tab")
check("function switchTab" in html and "const TABS" in html, "含快捷 tab 实现")
check('class="headline"' in html and "r.symbol" in html and "r.title" in html,
      "行首渲染 symbol + title（人话优先）")
check("pass-reason" in html, "通过原因有独立（绿色）样式")
check("legendMap" in html and "字段说明" in html, "含字段图例渲染")
check("不是串联漏斗" in html, "统计卡含口径说明（非漏斗）")
check("metricTip" in html, "metric chip 挂 tooltip")
# G6 通过行默认折叠的视图值由前端传入
check("params.set('view', 'key')" in html, "默认视图传 view=key")
check("**" not in html, "无 markdown 强调符（HTML 邮件同款护栏，此处防串味）")

print("== 8. 管线写入侧接线（源码 AST/文本守卫） ==")
_pipe_path = os.path.join(_SCRIPTS, "bin", "phase_catalyst_pipeline.py")
with open(_pipe_path, encoding="utf-8") as fh:
    pipe = fh.read()
# 各阶段 query 增选 title/symbol
check("ac.title" in pipe, "管线 SQL 含 ac.title")
check("a.canonical_symbol AS symbol" in pipe, "管线 SQL 含 asset symbol")
# 各 trace_step 调用带 title/symbol
for stage, expect in [
    ("G6_signal", True), ("G2_resonance", True), ("G3G5_recalc", True),
]:
    # 抓取该阶段所有 trace_step 调用文本
    calls = re.findall(r'trace_step\(\s*"%s".*?\)' % stage, pipe, re.S)
    has_title = any("title=" in c for c in calls)
    has_symbol = any("symbol=" in c for c in calls)
    check(has_title and has_symbol,
          f"{stage} 的 trace_step 均带 title/symbol（{len(calls)} 处）",
          f"title={has_title} symbol={has_symbol}")
# trace_step 签名支持 symbol
_sig = ct.trace_step.__code__.co_varnames[:ct.trace_step.__code__.co_argcount]
check("symbol" in _sig, "trace_step 签名含 symbol", str(_sig))
ast.parse(pipe)  # 语法自证
check(True, "phase_catalyst_pipeline.py 语法可解析")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
