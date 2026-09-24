#!/usr/bin/env python3
"""投研页「数据完整度」徽章护栏（工单 OPT-UI-001，纯离线）。

运行：python workbench/test_research_completeness_badge.py

判据：
  1. research.html 头部含 `#r-completeness` 容器 + `.r-completeness` 样式；
  2. `render(d)` 调用 `renderCompleteness(d)`，且其定义以 notebook 的 `missing`
     为唯一数据源（`m.present` 计数），tooltip 用「暂无数据」列缺失项；
  3. 该函数**不含任何 fetch/外部请求**（工单约束：COINGLASS 仅 Hobbyist、
     其余免费额度 ⇒ 展示层不得新增配额消耗）；
  4. 侧栏缺失项文案统一为「⚠ 暂无数据」（与徽章口径一致）；
  5. 后端 notebook 返回体含 `"missing"`（缺失清单来源）。
"""
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE = os.path.join(_HERE, "templates", "research.html")
_DB_STATS = os.path.join(_HERE, "db_stats.py")

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


def _extract_js_function(src: str, name: str) -> str:
    """按大括号配对提取 `function name (...) { ... }` 的完整文本。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    start = src.index("{", m.end() - 1) if "{" in src[m.end():m.end() + 200] else src.index("{", m.start())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return ""


html = open(_TEMPLATE, encoding="utf-8").read()
db_src = open(_DB_STATS, encoding="utf-8").read()

print("\n【1】容器与样式")
check('id="r-completeness"' in html, "头部存在 #r-completeness 容器")
check(re.search(r"\.r-completeness\s*\{", html) is not None, ".r-completeness 样式已定义")

print("\n【2】接线：render() 调用 + 定义存在")
check("renderCompleteness(d);" in html, "render(d) 调用 renderCompleteness")
fn = _extract_js_function(html, "renderCompleteness")
check(bool(fn), "renderCompleteness 函数体可提取")

print("\n【3】口径：以 missing/present 为准，tooltip 列缺失项")
check("d.missing" in fn, "数据源为 d.missing")
check("!m.present" in fn, "以 m.present 过滤缺失项")
check("数据完整度" in fn, "显示「数据完整度」")
check("暂无数据" in fn, "tooltip 使用「暂无数据」措辞")
check("ok / total" in fn, "进度按 present/total 计算")

print("\n【4】零配额：徽章不做任何外部请求")
check("fetch(" not in fn, "renderCompleteness 内无 fetch（零外部请求）")
check("XMLHttpRequest" not in fn, "renderCompleteness 内无 XHR")

print("\n【5】侧栏文案统一")
check("'⚠ 暂无数据'" in html, "renderSidebar 缺失项文案为「⚠ 暂无数据」")

print("\n【6】后端返回 missing 清单")
check('"missing": missing' in db_src, "notebook 返回体含 missing 字段")
check("_compute_missing_materials" in db_src, "missing 由 _compute_missing_materials 生成")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
