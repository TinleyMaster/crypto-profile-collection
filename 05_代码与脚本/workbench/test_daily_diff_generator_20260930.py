#!/usr/bin/env python3
"""daily_diff_generator 幂等替换 + 前端标签回归护栏（审计_每日变化榜_2026-09-30）。

运行：python test_daily_diff_generator_20260930.py

钉住的不变量：
  F1（P0）：同日重跑必须「先删当日该类别再插入」，否则 ON CONFLICT DO NOTHING 只增
           不改 ⇒ data_sync_daily + daily_diff_fallback 两次运行混存 ⇒ 行数 > LIMIT、
           rank 重复（prod 09-29 实测 created_at 两个时刻、price_change_24h up n=61）。
  F3（P2）：前端 DIFF_CATEGORY_LABELS 必须覆盖生成器产出的全部类别（此前漏
           market_cap_mover / tvl_surge_24h ⇒ Tab 显示英文兜底）。

⚠️ 纯离线：假 cursor，不连库、不打网络。
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
_SRC = os.path.join(os.path.dirname(_HERE), "scripts", "src")
for p in (_BIN, _HERE, _SRC):
    if p not in sys.path:
        sys.path.insert(0, p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import daily_diff_generator as G  # noqa: E402

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


print("== 1. GENERATED_CATEGORIES 与 INSERT SQL 一致 ==")
_src_path = os.path.join(_BIN, "daily_diff_generator.py")
_src = open(_src_path, encoding="utf-8").read()
_cats_in_sql = set()
# SELECT 型插入（列清单后可能跟 WITH ... CTE，故允许中间有 WITH 段）
for m in re.finditer(
        r"INSERT INTO biz\.daily_diff_summary\s*\([^)]*\)\s*(?:WITH\b.*?)?SELECT\s*%s::DATE,\s*'([a-z0-9_]+)'",
        _src, re.S):
    _cats_in_sql.add(m.group(1))
# VALUES 型插入（sector_rotation）
for m in re.finditer(
        r"INSERT INTO biz\.daily_diff_summary\s*\([^)]*\)\s*VALUES\s*\(%s,\s*'([a-z0-9_]+)'",
        _src):
    _cats_in_sql.add(m.group(1))
check(_cats_in_sql == set(G.GENERATED_CATEGORIES),
      "GENERATED_CATEGORIES == 全部 INSERT 写入的类别（无遗漏/无多余）",
      f"sql={sorted(_cats_in_sql)} const={sorted(G.GENERATED_CATEGORIES)}")
check(len(G.GENERATED_CATEGORIES) == 10, "共 10 个类别", str(len(G.GENERATED_CATEGORIES)))


class _Cur:
    """假游标：记录 execute，fetchone 恒 (0,)、fetchall 恒 []（不触发条件分支）。"""

    def __init__(self):
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.rowcount = 0

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


print("== 2. F1：generate_for_date 先删当日该类别再插入 ==")
cur = _Cur()
G.generate_for_date(cur, date(2026, 9, 29))
check(len(cur.calls) > 1, "发生了多次 execute", str(len(cur.calls)))

_first_sql = cur.calls[0][0].strip()
check(_first_sql.upper().startswith("DELETE FROM BIZ.DAILY_DIFF_SUMMARY"),
      "第一条语句是 DELETE（先清空）", _first_sql[:80])
check("ANY(%s)" in _first_sql, "DELETE 按类别列表 ANY(%s) 精确清空")
check(cur.calls[0][1] == ("2026-09-29", list(G.GENERATED_CATEGORIES)),
      "DELETE 参数 = (日期, 生成器负责的全部类别)", str(cur.calls[0][1]))

_insert_idxs = [i for i, (sql, _) in enumerate(cur.calls)
                if "INSERT INTO biz.daily_diff_summary" in sql]
check(bool(_insert_idxs), "存在 INSERT", str(len(_insert_idxs)))
check(all(i > 0 for i in _insert_idxs),
      "所有 INSERT 都在 DELETE 之后（删除不能晚于插入）", str(_insert_idxs))

print("== 3. 源码护栏 ==")
check("GENERATED_CATEGORIES = (" in _src, "源码定义 GENERATED_CATEGORIES")
check("DELETE FROM biz.daily_diff_summary WHERE diff_date = %s AND category = ANY(%s)" in _src,
      "generate_for_date 内含幂等删除语句")

print("== 4. F3：前端 DIFF_CATEGORY_LABELS 覆盖全部类别 ==")
_html_path = os.path.join(_HERE, "templates", "index.html")
_html = open(_html_path, encoding="utf-8").read()
_m = re.search(r"const DIFF_CATEGORY_LABELS = \{(.*?)\};", _html, re.S)
check(bool(_m), "找到 DIFF_CATEGORY_LABELS 定义")
_labels_src = _m.group(1) if _m else ""
_missing = [c for c in G.GENERATED_CATEGORIES if f"'{c}'" not in _labels_src]
check(not _missing, "DIFF_CATEGORY_LABELS 覆盖生成器全部类别（含 market_cap_mover）",
      f"缺失: {_missing}")
check("'market_cap_mover': '市值变化榜'" in _labels_src, "market_cap_mover 有中文标签")
check("'tvl_surge_24h'" in _labels_src, "tvl_surge_24h 有中文标签")
# DIFF_CATEGORY_TIP 同步补键（有值即可，避免 Tab tooltip 落英文）
_mt = re.search(r"const DIFF_CATEGORY_TIP = \{(.*?)\};", _html, re.S)
_tip_src = _mt.group(1) if _mt else ""
_tip_missing = [c for c in G.GENERATED_CATEGORIES if f"'{c}'" not in _tip_src]
check(not _tip_missing, "DIFF_CATEGORY_TIP 同步覆盖生成器类别", f"缺失: {_tip_missing}")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
