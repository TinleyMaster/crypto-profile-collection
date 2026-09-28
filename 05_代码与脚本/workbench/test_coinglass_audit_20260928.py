#!/usr/bin/env python3
"""CoinGlass V4 对账脚本护栏单测（Coinglass套餐数据接入方案 §6.4）。

运行：python test_coinglass_audit_20260928.py

钉住的不变量：
  ① **只读**：脚本 SQL 常量不得含 INSERT/UPDATE/DELETE/DROP/ALTER（纯对账，不写任何表）；
  ② **CLI 契约**：`--symbols` / `--limit` / `--hours` / `--json` / `--probe` 齐备（§6.4 表）；
  ③ **判定规则**：p90 绝对差异 < T 且同向率 > 80% ⇒ injectable；否则 monitor_only；
     缺样本 ⇒ insufficient_data（缺失≠0，不臆断）；阈值 T 只留代码常量（不在文档留数字）；
     ⚠️ 「同向率」= **两源同正负占比**（`_same_sign`），**非**差值 `CG-BN` 的正负主导
     （后者会把「两源符号相反」误算成同向——见测试3b 回归护栏）；
  ④ **符号口径**：合约码 → 币种基码映射与 ingest 同逻辑（BTCUSDT→BTC / 1000PEPEUSDT→1000PEPE）；
  ⑤ **口径隔离**：coinglass 取 `exchange='All'` 聚合行；Binance 取 `row_number()` 最新一行。

⚠️ 本文件不连库、不打网络（仅源码/纯函数断言）。
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_BIN = os.path.join(_SCRIPTS, "bin")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, _BIN)
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

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


AUDIT = os.path.join(_BIN, "phase_audit_coinglass_vs_binance.py")
with open(AUDIT, encoding="utf-8") as fh:
    _SRC = fh.read()

import phase_audit_coinglass_vs_binance as au  # noqa: E402  （模块级只定义常量/函数，不连库）


# ════════════════════════════════════════════════════════════
# 1. 不变量①：纯只读（SQL 常量不得含写操作）
# ════════════════════════════════════════════════════════════
print("\n【测试1】不变量①：对账脚本纯只读（不写任何表）")
_WRITE_KW = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ")
_sql_consts: list[tuple[str, str]] = []
for _n in ast.parse(_SRC).body:
    if (isinstance(_n, ast.Assign) and isinstance(_n.value, ast.Constant)
            and isinstance(_n.value.value, str) and "SELECT" in _n.value.value):
        for _t in _n.targets:
            if isinstance(_t, ast.Name):
                _sql_consts.append((_t.id, _n.value.value))
check(len(_sql_consts) >= 2, "SQL 常量可枚举（守卫非空转）", str([n for n, _ in _sql_consts]))
for _name, _sql in _sql_consts:
    check(not any(k in _sql.upper() for k in _WRITE_KW),
          f"{_name} 不含任何写操作关键字（SELECT-only）", _sql[:60])
check("SELECT" in au.SQL_COINGLASS_LATEST and "SELECT" in au.SQL_BINANCE_LATEST,
      "正向对照：两条 SQL 均为 SELECT（上面的否定断言非空转）")


# ════════════════════════════════════════════════════════════
# 2. 不变量②：CLI 契约（§6.4 表）
# ════════════════════════════════════════════════════════════
print("\n【测试2】不变量②：CLI 契约齐备（--symbols / --limit / --hours / --json / --probe）")
for _flag in ("--symbols", "--limit", "--hours", "--json", "--probe"):
    check(f'"{_flag}"' in _SRC, f"CLI 含 {_flag}")
check(f"default=DEFAULT_LOOKBACK_H" in _SRC and au.DEFAULT_LOOKBACK_H == 48,
      "--hours 默认 = 48h（覆盖 2 个日频采集周期）", str(au.DEFAULT_LOOKBACK_H))
check("selected_bases" in _SRC and "args.limit" in _SRC,
      "--symbols / --limit 实际参与配对收敛（非死参数）")
check("sys.exit(main())" in _SRC and "__main__" in _SRC,
      "入口受 __main__ 保护（import 不连库、不执行对账）")


def _module_level_calls(tree, names=("get_connection", "get_settings")) -> list[str]:
    """模块顶层（函数/类定义之外）对 DB 入口的调用 —— 应为空，否则 import 即连库。"""
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id in names):
                found.append(sub.func.id)
    return found


check(not _module_level_calls(ast.parse(_SRC)),
      "模块顶层无 get_connection/get_settings 调用（import 即安全，测试纯度守卫）",
      str(_module_level_calls(ast.parse(_SRC))))


# ════════════════════════════════════════════════════════════
# 3. 不变量③：判定规则（纯函数 judge）
# ════════════════════════════════════════════════════════════
print("\n【测试3】不变量③：判定规则（阈值常量 + 三态）")
check(au.FUNDING_ABS_DIFF_P90_T > 0 and au.SAME_DIRECTION_MIN_PCT == 80.0,
      "阈值 T / 同向率下限为代码常量（T 不在文档留数字）",
      f"T={au.FUNDING_ABS_DIFF_P90_T} same={au.SAME_DIRECTION_MIN_PCT}")

_inj = au.judge({"p90": au.FUNDING_ABS_DIFF_P90_T / 2, "same_direction_pct": 90.0},
                {"p90": 0.1})
check(_inj["verdict"] == "injectable", "p90 < T 且同向率 > 80% ⇒ injectable", str(_inj))
check(_inj["rel_p90"] == 0.1 and _inj["t"] == au.FUNDING_ABS_DIFF_P90_T,
      "判定结果回带 rel_p90 / 阈值供审计复核", str(_inj))

_hi = au.judge({"p90": au.FUNDING_ABS_DIFF_P90_T, "same_direction_pct": 90.0}, {"p90": 0.1})
check(_hi["verdict"] == "monitor_only", "p90 == T（严格 <，边界不通过）⇒ monitor_only", str(_hi))
_hi2 = au.judge({"p90": au.FUNDING_ABS_DIFF_P90_T / 2, "same_direction_pct": 80.0}, {"p90": 0.1})
check(_hi2["verdict"] == "monitor_only", "同向率 == 80%（严格 >，边界不通过）⇒ monitor_only", str(_hi2))
_low_dir = au.judge({"p90": 0.0, "same_direction_pct": 50.0}, {"p90": 0.1})
check(_low_dir["verdict"] == "monitor_only", "同向率过低（50%）⇒ monitor_only（不误判可注入）",
      str(_low_dir))

for _missing in (
    {"p90": None, "same_direction_pct": 90.0},
    {"p90": 0.001, "same_direction_pct": None},
    {},
):
    _v = au.judge(_missing, {"p90": None})
    check(_v["verdict"] == "insufficient_data",
          f"缺样本 {_missing} ⇒ insufficient_data（缺失≠0，不臆断方向）", str(_v))
check("judgment" in _SRC and "judge(funding_diff, funding_rel)" in _SRC,
      "判定结果实际进入输出（result['judgment']，非只算不算）")


# ── 3b. 同向率语义：两源同正负（而非差值 CG-BN 的正负主导）─────────
print("\n【测试3b】同向率 = 两源同正负占比（P1 修复回归护栏）")
check(au._same_sign(0.0001, 0.0002) is True and au._same_sign(-0.0001, -0.0002) is True,
      "同为正 / 同为负 ⇒ 同向")
check(au._same_sign(0.0001, -0.0001) is False, "符号相反 ⇒ 不同向")
check(au._same_sign(0.0, 0.0001) is False and au._same_sign(0.0, 0.0) is False,
      "任一为 0 ⇒ 不算同向（方向不可判）")

# 复现审计原例：两源符号相反 ⇒ 差值 cg-bn 恒为正，旧算法（差值的正负主导）会误判 100% 同向
_pairs = [
    {"funding_rate_abs_diff": 0.0002, "funding_same_sign": False},   # cg=+0.0001 bn=-0.0001
    {"funding_rate_abs_diff": 0.0002, "funding_same_sign": False},
    {"funding_rate_abs_diff": 0.0001, "funding_same_sign": True},
    {"funding_rate_abs_diff": 0.0001, "funding_same_sign": True},
]
_s = au.summarize_pairs(_pairs, "funding_rate_abs_diff", "funding_same_sign")
check(_s["same_direction_pct"] == 50.0,
      "同向率按「两源同正负」标志计（2/4=50%），非差值正负主导（旧算法会得 100%）", str(_s))
check(abs(_s["p90"] - 0.0002) < 1e-12 and _s["n"] == 4,
      "分位仍基于差值（p90=0.0002）——只修同向率口径，不动统计口径", str(_s))

_dir = au.judge({"p90": 0.0001, "same_direction_pct": 0.0}, {"p90": 0.1})
check(_dir["verdict"] == "monitor_only",
      "两源全符号相反（同向率 0%）⇒ monitor_only（不因 p90<T 误判 injectable）", str(_dir))

_noflag = au.summarize_pairs(_pairs, "funding_rate_abs_diff")
check(_noflag["same_direction_pct"] is None, "无 flag_key ⇒ 同向率 None（缺失≠0）")
check(au.summarize_pairs([], "funding_rate_abs_diff", "funding_same_sign")["same_direction_pct"] is None,
      "无样本 ⇒ 同向率 None（不返回 0 冒充）")


# ════════════════════════════════════════════════════════════
# 4. 不变量④：符号口径（与 ingest 同逻辑）
# ════════════════════════════════════════════════════════════
print("\n【测试4】不变量④：合约码 → 币种基码（与 ingest 同逻辑）")
check(au.base_code("BTCUSDT") == "BTC" and au.base_code("1000PEPEUSDT") == "1000PEPE",
      "BTCUSDT→BTC / 1000PEPEUSDT→1000PEPE（前缀币保留）",
      f"{au.base_code('BTCUSDT')} / {au.base_code('1000PEPEUSDT')}")
check(au.base_code("BTCUSDC") == "BTC" and au.base_code("BTCBUSD") == "BTC",
      "USDC / BUSD 计价后缀同样剥离", str(au.base_code("BTCUSDC")))
check(au.base_code("BTC") == "BTC" and au.base_code("btcusdt") == "BTC",
      "无后缀原样 / 大小写无关", str(au.base_code("btcusdt")))


# ════════════════════════════════════════════════════════════
# 5. 不变量⑤：口径隔离（'All' 聚合行；Binance 取最新一行）
# ════════════════════════════════════════════════════════════
print("\n【测试5】不变量⑤：两源取数口径")
check("exchange = 'All'" in au.SQL_COINGLASS_LATEST,
      "coinglass 侧只取 'All' 跨所聚合行（不与分所行相加）", au.SQL_COINGLASS_LATEST[:120])
check("ORDER BY ts DESC" in au.SQL_COINGLASS_LATEST,
      "coinglass 侧按 ts 取每币最新值（非跨桶求和）")
check("row_number() OVER (PARTITION BY symbol ORDER BY fetched_at DESC)" in au.SQL_BINANCE_LATEST,
      "Binance 侧 row_number() 取每币最新一行（快照表 PK=asset_id，无时间序列）")
check("total_oi_usd" in au.SQL_BINANCE_LATEST,
      "Binance OI 取 asset_derivatives.total_oi_usd（§6.4 指定列）")
check("make_interval(hours => %s)" in au.SQL_COINGLASS_LATEST
      and "make_interval(hours => %s)" in au.SQL_BINANCE_LATEST,
      "--hours 同时作用于两源（同窗口对账）")
check("oi_usd_rel_diff" in _SRC and "funding_rate_abs_diff" in _SRC
      and "funding_rate_rel_diff" in _SRC,
      "三项指标齐备（abs / rel / oi）")


# ════════════════════════════════════════════════════════════
# 6. 纯函数 pctile 边界
# ════════════════════════════════════════════════════════════
print("\n【测试6】pctile 边界（空样本 / 单点 / 插值）")
check(au.pctile([], 0.5) is None, "空样本 ⇒ None（不返回 0 冒充分位）")
check(au.pctile([3.0], 0.5) == 3.0 and au.pctile([3.0], 0.9) == 3.0, "单点 ⇒ 恒该值")
check(au.pctile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0, "5 点中位 = 3.0")
check(abs(au.pctile([0.0, 10.0], 0.5) - 5.0) < 1e-12, "线性插值：2 点中位 = 5.0")
check(au.pctile([1.0, 2.0, 3.0], 0.0) == 1.0 and au.pctile([1.0, 2.0, 3.0], 1.0) == 3.0,
      "p0 / p100 取端点（不越界）")
check(inspect.isfunction(au.judge) and inspect.isfunction(au.pctile)
      and inspect.isfunction(au.base_code),
      "judge / pctile / base_code 均为可单测纯函数")


# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)