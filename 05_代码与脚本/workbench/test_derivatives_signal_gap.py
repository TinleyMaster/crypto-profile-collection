#!/usr/bin/env python3
"""衍生生品费率覆盖缺口 O1 修复探针（待修复工单_O1_费率覆盖缺口_2026-09-22.md）。

运行：python workbench/test_derivatives_signal_gap.py
      （源码/归一化断言离线恒可跑；能连生产库时自动追加**只读**缺口枚举，连不上只跳过）

覆盖：
  1. `_signal_symbol_candidates` 合约符号 → 裸符号归一（B2USDT/1000FLOKIUSDT/…）；
  2. 源码 AST：`--signal-days` 参数存在、`main` 合并信号缺口且缺口不被 --limit 截断；
  3. `get_signal_gap_assets` 查询语义（作用域 main/BRK + NOT EXISTS 无衍生品行）；
  4. 只读 prod：缺口集合确实「无 asset_derivatives 行」；`--signal-days 0` 恒空。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import phase_derivatives_batch as pdb  # noqa: E402

passed = 0
failed = 0
skipped = 0


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


def skip(name, reason):
    global skipped
    skipped += 1
    print(f"  \u2013 跳过 {name}：{reason}")


SRC = open(pdb.__file__, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _func(name):
    return next((n for n in TREE.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


# ═══════════════════════════════════════════════════════════════
#  1. 符号归一化
# ═══════════════════════════════════════════════════════════════

print("\n【1】合约符号 → 裸符号候选（与 scan_daemon 同口径）")
cases = {
    "B2USDT": ["B2USDT", "B2"],
    "EPICUSDT": ["EPICUSDT", "EPIC"],
    "1000FLOKIUSDT": ["1000FLOKIUSDT", "1000FLOKI", "FLOKI"],
    "1000000MOGUSDT": ["1000000MOGUSDT", "1000000MOG", "MOG"],
    "BROCCOLI714USDT": ["BROCCOLI714USDT", "BROCCOLI714"],
    "BTCUSDT": ["BTCUSDT", "BTC"],
}
for sym, want in cases.items():
    got = pdb._signal_symbol_candidates(sym)
    check(got == want, f"{sym} → {want}", f"got={got}")

# ═══════════════════════════════════════════════════════════════
#  2. 源码 AST：参数 + 合并拓扑
# ═══════════════════════════════════════════════════════════════

print("\n【2】源码形状（AST）")
main_fn = _func("main")
main_src = ast.unparse(main_fn) if main_fn else ""
check(main_fn is not None, "找到 phase_derivatives_batch.main()")
check("--signal-days" in main_src, "main 定义/使用 --signal-days 参数")
check("get_signal_gap_assets" in main_src, "main 调用 get_signal_gap_assets（O1 缺口对齐）")
check("gap_assets" in main_src and "gap_ids" in main_src,
      "缺口资产置顶去重（gap_assets + gap_ids）")
check("max(args.limit, len(gap_assets))" in main_src,
      "缺口不被 --limit 截断（cap = max(limit, 缺口数)）")

gap_fn = _func("get_signal_gap_assets")
gap_src = ast.unparse(gap_fn) if gap_fn else ""
check(gap_fn is not None, "找到 get_signal_gap_assets()")
check("pool = 'main' OR scenario = 'BRK'" in gap_src,
      "作用域限定主池/BRK（funding 的消费方；squeeze 池不用 funding）")
check("NOT EXISTS" in gap_src and "asset_derivatives" in gap_src,
      "只纳入「从无 asset_derivatives 行」的资产（稳态增量有限）")
check("scan_signal" in gap_src, "数据源为 biz.scan_signal")

# ═══════════════════════════════════════════════════════════════
#  3. --signal-days 0 关闭（离线，无需连库）
# ═══════════════════════════════════════════════════════════════

print("\n【3】--signal-days 0 关闭 O1 对齐")
check(pdb.get_signal_gap_assets(None, 0) == [],
      "days=0 → 直接返回 []（不触库、不改变原行为）")

# ═══════════════════════════════════════════════════════════════
#  4. 只读 prod：缺口确实无衍生品行（连不上则跳过）
# ═══════════════════════════════════════════════════════════════

print("\n【4】只读 prod：缺口集合不变量")
try:
    import psycopg.rows  # noqa: E402
    from crypto_research.config import get_settings  # noqa: E402
    from crypto_research.db.conn import get_connection  # noqa: E402

    with get_connection(get_settings().database_url) as conn:
        gaps = pdb.get_signal_gap_assets(conn, 7)
        ids = [g["asset_id"] for g in gaps]
        if ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM biz.asset_derivatives WHERE asset_id = ANY(%s)",
                    (ids,),
                )
                leaked = cur.fetchone()[0]
        else:
            leaked = 0
except Exception as exc:  # noqa: BLE001
    skip("只读 prod 缺口不变量", f"无法连库：{type(exc).__name__}: {exc}")
    gaps = None

if gaps is not None:
    print(f"    （近 7 天主池/BRK 信号缺口：{len(gaps)} 个，样例："
          f"{[g['symbol'] for g in gaps[:6]]}）")
    check(leaked == 0,
          "缺口集合与 asset_derivatives 零交集（函数语义自洽）",
          f"{leaked} 个缺口资产竟已有衍生品行")
    ranks = [g["market_cap_rank"] for g in gaps if g["market_cap_rank"] is not None]
    check(all(g["market_cap_rank"] is None or g["market_cap_rank"] > 100
              for g in gaps) or not gaps,
          "缺口多为 rank>100 或 rank 缺失（正是原 top-100 采集漏掉的）",
          f"ranks={sorted(ranks)[:10]}")

print(f"\n结果：{passed} 通过 / {failed} 失败 / {skipped} 跳过")
sys.exit(1 if failed else 0)
