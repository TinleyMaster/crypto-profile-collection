#!/usr/bin/env python3
"""task_manager 日志链路韧性护栏单测（修 catalyst_run_all 周期性 stuck，2026-09-29）。

运行：python test_task_manager_log_resilience.py

钉住的不变量：
  ① **逐行写库失败不拖垮读取线程**：`_safe_append_log` 失败返回 False 且**不抛**，
     使 `for raw_line in proc.stdout` 能继续 drain（否则管道填满会冻住子进程）；
  ② **连接池抗半开**：`_get_pool` 的 kwargs 含 TCP keepalives + statement_timeout +
     idle_in_transaction 超时；否则远端连接半开时逐行写会在协议层永久挂起；
  ③ **读取循环接线**：reader 用 `_safe_append_log`、`_try_parse_stats` 有 try 兜底；
  ④ **收割仍杀进程**：reaper 对被收割任务调用 `_kill_proc_tree`。

⚠️ 本文件不连库、不打网络（纯函数 + 源码/AST 断言）。
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
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


TM_PATH = os.path.join(_HERE, "task_manager.py")
with open(TM_PATH, encoding="utf-8") as fh:
    _SRC = fh.read()

import task_manager as tm  # noqa: E402  （模块级不连库）


# ════════════════════════════════════════════════════════════
# 1. _safe_append_log 行为：成功透传 / 失败不抛 / 计数
# ════════════════════════════════════════════════════════════
print("\n【测试1】_safe_append_log：失败不抛、可继续 drain")
_calls = []
_orig = tm._append_log

tm._append_log = lambda tid, line: _calls.append((tid, line))
_st = {"log_failures": 0}
check(tm._safe_append_log("t1", "hello", _st) is True, "成功写库返回 True")
check(_calls == [("t1", "hello")] and _st["log_failures"] == 0, "成功不计失败数")


def _boom(tid, line):
    raise RuntimeError("simulated db half-open")


tm._append_log = _boom
_st = {"log_failures": 0}
_raised = False
try:
    for i in range(5):
        ok = tm._safe_append_log("t1", f"line{i}", _st)
        if ok:
            raise AssertionError("失败行不应返回 True")
except Exception:
    _raised = True
check(not _raised, "连写 5 行全失败也不抛异常（读取线程可继续 drain）")
check(_st["log_failures"] == 5, "每行失败都计数", str(_st))
check(tm._safe_append_log("t1", "x") is False, "不传 state 也不抛、返回 False")

tm._append_log = _orig


# ════════════════════════════════════════════════════════════
# 2. 连接池抗半开（keepalives + 超时）
# ════════════════════════════════════════════════════════════
print("\n【测试2】_get_pool 连接参数（keepalives / statement_timeout / idle-in-tx）")
_pool_src = ""
try:
    _pool_src = inspect.getsource(tm._get_pool)
except Exception:
    _pool_src = ""
check('"keepalives": 1' in _pool_src and '"keepalives_idle"' in _pool_src
      and '"keepalives_interval"' in _pool_src and '"keepalives_count"' in _pool_src,
      "连接池开启 TCP keepalives（半开连接 ~60s 内报错而非永久挂起）")
check("statement_timeout" in _pool_src and "idle_in_transaction_session_timeout" in _pool_src,
      "options 含 statement_timeout + idle_in_transaction_session_timeout")
check("lock_timeout=30000" in _pool_src, "原有 lock_timeout 保留（不回归）")


# ════════════════════════════════════════════════════════════
# 3. 读取循环接线
# ════════════════════════════════════════════════════════════
print("\n【测试3】_run_task 读取循环接线")
check("_safe_append_log(task_id, line, log_state)" in _SRC,
      "reader 用 _safe_append_log（非裸 _append_log）")
check("for raw_line in proc.stdout:" in _SRC,
      "读取循环仍在（结构性守卫）")
check("stats 解析失败" in _SRC, "_try_parse_stats 有 try 兜底（单行 stats 异常不致命）")
check("log_state = {\"log_failures\": 0}" in _SRC, "读取前初始化 log_state")


# ════════════════════════════════════════════════════════════
# 4. 收割仍杀进程 + _safe_append_log 定义在 _read_log 之前（可被引用）
# ════════════════════════════════════════════════════════════
print("\n【测试4】收割杀进程 & 定义顺序")
check("_kill_proc_tree(proc)" in _SRC,
      "reaper 对被收割任务调用 _kill_proc_tree（僵尸进程不再残留）")
check(_SRC.index("def _safe_append_log") < _SRC.index("def _read_log"),
      "_safe_append_log 定义在 _read_log 之前（模块级可被引用）")


# ════════════════════════════════════════════════════════════
# 5. 共享脚本连接池（conn.py）同样抗半开
# ════════════════════════════════════════════════════════════
print("\n【测试5】conn.py 连接池 TCP keepalives（同类加固，覆盖子脚本自身连接）")
CONN = os.path.join(_SCRIPTS, "src", "crypto_research", "db", "conn.py")
with open(CONN, encoding="utf-8") as fh:
    _CONN_SRC = fh.read()
check('"keepalives": 1' in _CONN_SRC and '"keepalives_idle"' in _CONN_SRC
      and '"keepalives_interval"' in _CONN_SRC and '"keepalives_count"' in _CONN_SRC,
      "conn.py 连接池开启 TCP keepalives")
check("lock_timeout=30000" in _CONN_SRC, "conn.py 原有 lock_timeout 保留（不回归）")


# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
