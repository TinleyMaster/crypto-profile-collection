#!/usr/bin/env python3
"""G4 流动性字面 0 值漏判修复探针（待修复工单_G4_流动性0值漏判_2026-09-22.md）。

运行：python workbench/test_fundamental_liquidity.py（纯离线）

判据：`_score_liquidity` 的守卫应与 `_score_tvl` 同口径 —— `None` 与 `<= 0` 都判 0；
不得让字面 `0` 落兜底分支（原 `return 20`，会与「有数据但极差」混淆）。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, _HERE)  # workbench → catalyst 包
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst.fundamental import FundamentalChecker  # noqa: E402

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


fc = FundamentalChecker({})  # 默认阈值 liq_good=5M / liq_ok=1M / liq_bad=100K

print("\n【G4-RESIDUAL-001】_score_liquidity 字面 0 与 None 同判 0")
check(fc._score_liquidity(None) == 0, "liq=None → 0（保持）", f"got={fc._score_liquidity(None)}")
check(fc._score_liquidity(0) == 0, "liq=0 → 0（本次修复，原为 20）", f"got={fc._score_liquidity(0)}")
check(fc._score_liquidity(0.0) == 0, "liq=0.0 → 0", f"got={fc._score_liquidity(0.0)}")
check(fc._score_liquidity(-1) == 0, "liq=-1 → 0（负值同判）", f"got={fc._score_liquidity(-1)}")

print("\n【零误伤】真实值阶梯不变")
check(fc._score_liquidity(50_000) == 20, "50K（>0 但 <100K）→ 20", f"got={fc._score_liquidity(50_000)}")
check(fc._score_liquidity(200_000) == 45, "200K → 45", f"got={fc._score_liquidity(200_000)}")
check(fc._score_liquidity(2_000_000) == 70, "2M → 70", f"got={fc._score_liquidity(2_000_000)}")
check(fc._score_liquidity(6_000_000) == 90, "6M → 90", f"got={fc._score_liquidity(6_000_000)}")

print("\n【口径一致】与 _score_tvl 同构")
check(fc._score_tvl(None, None) == 0, "tvl=None → 0", f"got={fc._score_tvl(None, None)}")
check(fc._score_tvl(0, None) == 0, "tvl=0 → 0", f"got={fc._score_tvl(0, None)}")
check(fc._score_tvl(2_000_000_000, None) == 90, "tvl=2B → 90", f"got={fc._score_tvl(2_000_000_000, None)}")

print("\n【源码】守卫为 `is None or <= 0`")
src = open(os.path.join(_HERE, "catalyst", "fundamental.py"), encoding="utf-8").read()
tree = ast.parse(src)
fn = next((n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_score_liquidity"), None)
fsrc = ast.unparse(fn) if fn else ""
check("liq_usd is None or liq_usd <= 0" in fsrc,
      "_score_liquidity 守卫含 `is None or <= 0`", f"src={fsrc[:120]}")

print("\n【Scope】_score_unlock 未改（字符串型，无 0 值）")
uf = next((n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_score_unlock"), None)
usrc = ast.unparse(uf) if uf else ""
check("if not pressure:" in usrc, "_score_unlock 保持 `if not pressure: return 50`（未纳入本次）")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
