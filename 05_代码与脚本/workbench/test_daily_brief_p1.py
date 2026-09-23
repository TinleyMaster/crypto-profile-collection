#!/usr/bin/env python3
"""早报 P1-A / P1-B 回归护栏（审计_加密大盘早报_2026-09-23）。

运行：python workbench/test_daily_brief_p1.py
      （纯离线；P1-A 直接断言格式化，P1-B 用源码守卫 + 只读文件检查）

  P1-A：_fmt_mcap 对 $1K~$10K 整数 K 取整误差过大（ETH $2,750→$3K，偏 ~9%）→ 改精确值。
  P1-B：whale_balance_change_7d_pct 为「Top10 集中度百分点变化」，合法域 [-100,100]；
        越界脏数据曾产出 -531% → 读取侧域夹取 + 生产者越界守卫。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS_BIN)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import send_daily_brief as sdb  # noqa: E402
import phase_chain_holder_scrape as phs  # noqa: E402

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


print("[P1-A] _fmt_mcap")
check(sdb._fmt_mcap(2750) == "$2,750", "ETH $2,750 不再显示 $3K", sdb._fmt_mcap(2750))
check(sdb._fmt_mcap(999) == "$999", "$999 保持", sdb._fmt_mcap(999))
check(sdb._fmt_mcap(9999) == "$9,999", "$9,999 精确", sdb._fmt_mcap(9999))
check(sdb._fmt_mcap(10000) == "$10K", "$10K 仍紧凑", sdb._fmt_mcap(10000))
check(sdb._fmt_mcap(86500) == "$86K", "BTC $86,500→$86K", sdb._fmt_mcap(86500))
check(sdb._fmt_mcap(556.4e6) == "$556.4M", "大额 M 口径不变", sdb._fmt_mcap(556.4e6))
check(sdb._fmt_mcap(None) == "N/A", "None 安全")

print("[P1-B] _valid_conc 行为 + 生产者守卫")
check(phs._valid_conc(85.19) == 85.19, "域内值保留")
check(phs._valid_conc(100.0) == 100.0, "上边界 100 保留")
check(phs._valid_conc(0.0) == 0.0, "下边界 0 保留")
check(phs._valid_conc(933.71) is None, "越界 933.71（SHRUB prod 样本）→ None")
check(phs._valid_conc(-5) is None, "负值 → None")
check(phs._valid_conc(None) is None, "None → None")
check(phs._valid_conc("abc") is None, "非数值 → None")

_mm_src = open(os.path.join(_HERE, "macro_market.py"), encoding="utf-8").read()
check("BETWEEN 2.0 AND 100.0" in _mm_src, "读取侧增持域夹取 [2,100]")
check("BETWEEN -100.0 AND -2.0" in _mm_src, "读取侧减持域夹取 [-100,-2]")
_prod_src = open(os.path.join(os.path.dirname(_HERE), "scripts", "bin",
                              "phase_chain_holder_scrape.py"), encoding="utf-8").read()
check("def _conc_delta" in _prod_src, "生产者 _conc_delta 越界守卫")
check("float(cur_top10) - float(prev7[1])" not in _prod_src, "旧的无界相减已移除")
check('"top10_concentration": _valid_conc(' in _prod_src, "生产者集中度写入前经 _valid_conc 校验")

print(f"\n{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
