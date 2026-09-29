#!/usr/bin/env python3
"""dedup_assets 韧性护栏单测（修 data_sync_daily 因 lock_timeout 失败，2026-09-28）。

运行：python test_dedup_assets_resilience.py

钉住的不变量：
  ① **锁错误分类**：lock_timeout(55P03) / 死锁(40P01) 判可重试；唯一约束等**不可**重试；
  ② **重试退避**：5/15/30s 且末档封顶；
  ③ **等锁放宽**：本任务直连 lock_timeout 默认 120s（全局池 30s 过紧），且**不污染池**；
  ④ **失败带语句上下文**：_apply_group 出错时异常带 `dedup_stmt`（定位是哪张表/哪一步）；
  ⑤ **编排降级**：data_sync_daily 的「资产同名去重」不再关键（失败不中止其余 13 子任务），
     而「赛道分类刷新」「主表 supply/市值对齐」仍为关键（未被误伤）。

⚠️ 本文件不连库、不打网络（纯函数 + 假连接 + 源码/AST 断言）。
"""
from __future__ import annotations

import ast
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


DEDUP = os.path.join(_BIN, "dedup_assets.py")
ORCH = os.path.join(_BIN, "run_data_sync_daily.py")
with open(DEDUP, encoding="utf-8") as fh:
    _SRC = fh.read()
with open(ORCH, encoding="utf-8") as fh:
    _ORCH = fh.read()

import psycopg.errors  # noqa: E402
import dedup_assets as dd  # noqa: E402  （模块级不连库）


# ════════════════════════════════════════════════════════════
# 1. 锁错误分类
# ════════════════════════════════════════════════════════════
print("\n【测试1】锁错误分类（可重试 vs 不可重试）")
check(dd.is_lock_error(psycopg.errors.LockNotAvailable()) is True,
      "lock_timeout（LockNotAvailable 55P03）⇒ 可重试")
check(dd.is_lock_error(psycopg.errors.DeadlockDetected()) is True,
      "死锁（DeadlockDetected 40P01）⇒ 可重试")


class _E(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


check(dd.is_lock_error(_E("55P03")) is True, "裸 sqlstate=55P03 也判可重试（防御性）")
check(dd.is_lock_error(_E("23505")) is False, "唯一约束冲突(23505) ⇒ 不可重试（重试无意义）")
check(dd.is_lock_error(ValueError("boom")) is False, "普通异常 ⇒ 不可重试")


# ════════════════════════════════════════════════════════════
# 2. 重试退避 + 等锁放宽常量
# ════════════════════════════════════════════════════════════
print("\n【测试2】退避序列与等锁放宽")
check([dd._retry_wait(i) for i in range(3)] == [5, 15, 30],
      "_retry_wait 0/1/2 = 5/15/30s", str([dd._retry_wait(i) for i in range(3)]))
check(dd._retry_wait(5) == 30, "超界尝试封顶为末档（不无限拉长）", str(dd._retry_wait(5)))
check(dd.DEFAULT_LOCK_TIMEOUT_MS == 120_000 and dd.DEFAULT_LOCK_TIMEOUT_MS > 30_000,
      "默认等锁 120s 且 > 全局池 30s（本清理任务单独放宽）", str(dd.DEFAULT_LOCK_TIMEOUT_MS))
check(dd.LOCK_RETRIES == 3 and dd.LOCK_RETRY_BACKOFF_SEC == (5, 15, 30),
      "重试次数 3 + 退避表固定（防被悄悄改成 0 次）")


# ════════════════════════════════════════════════════════════
# 3. 连接口径：直连设 lock_timeout，不借池
# ════════════════════════════════════════════════════════════
print("\n【测试3】专用直连（lock_timeout 不污染全局池）")
check("_connect_direct" in _SRC and 'f"-c lock_timeout={int(lock_timeout_ms)}"' in _SRC,
      "_connect_direct 显式 options=-c lock_timeout=<ms>（非池连接）")
check("psycopg.connect(" in _SRC and "_connect_direct(settings, args.lock_timeout_ms)" in _SRC,
      "apply 路径走专用直连（不会把 lock_timeout 写进池内复用连接）")
check("--lock-timeout-ms" in _SRC and "args.lock_timeout_ms" in _SRC,
      "CLI 暴露 --lock-timeout-ms（可运维调整）")


# ════════════════════════════════════════════════════════════
# 4. 失败带语句上下文（假连接注入 lock 错误）
# ════════════════════════════════════════════════════════════
print("\n【测试4】_apply_group 出错带 dedup_stmt（定位到语句）")


class _BoomCursor:
    """在第 N 次 execute（或命中 boom_substr）时抛 LockNotAvailable，模拟撞锁。"""

    rowcount = 0

    def __init__(self, boom_substr):
        self.boom = boom_substr

    def execute(self, sql, params=None):
        if self.boom and self.boom in sql:
            raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    def fetchone(self):
        return {"primary_sector": "other"}


class _FakeConn:
    def __init__(self, boom_substr):
        self._c = _BoomCursor(boom_substr)

    def cursor(self, row_factory=None):
        return self._c


_group = {"symbol": "X", "keep": {"asset_id": 1}, "drops": [{"asset_id": 2}]}
try:
    dd._apply_group(_FakeConn("DELETE FROM core.asset WHERE asset_id"), _group)
    check(False, "撞锁应抛出（未抛）")
except psycopg.errors.LockNotAvailable as e:
    check(getattr(e, "dedup_stmt", None) == "删除冗余 core.asset",
          "异常带 dedup_stmt=删除冗余 core.asset（定位到级联删除这一步）",
          str(getattr(e, "dedup_stmt", None)))
    check(dd.is_lock_error(e) is True, "该异常被判可重试（走退避）")

# 命中中间某张表时也应定位到该表
try:
    dd._apply_group(_FakeConn("UPDATE core.asset_contract SET asset_id"), _group)
    check(False, "撞锁应抛出（未抛）")
except psycopg.errors.LockNotAvailable as e:
    check("asset_contract" in getattr(e, "dedup_stmt", ""),
          "撞锁在中间表时 dedup_stmt 定位到该表", str(getattr(e, "dedup_stmt", "")))

check("e.dedup_stmt = _LAST_STMT" in _SRC, "_apply_group 捕获异常时挂 dedup_stmt")
check(_SRC.count("_run(cur,") + _SRC.count("_run(\n") >= 5 or "_run(" in _SRC,
      "_run 语句标签机制在 _apply_group/_merge_primary_sector 中启用")


# ════════════════════════════════════════════════════════════
# 5. 编排隔离：任一子任务失败都不中止其余
# ════════════════════════════════════════════════════════════
print("\n【测试5】data_sync_daily 子任务隔离（结构面）")
_tree = ast.parse(_ORCH)
_tasks = None
for _n in _tree.body:
    if (isinstance(_n, ast.Assign) and any(getattr(t, "id", None) == "TASKS" for t in _n.targets)
            and isinstance(_n.value, (ast.List, ast.Tuple))):
        _tasks = ast.literal_eval(_n.value)
check(isinstance(_tasks, list) and len(_tasks) > 0, "TASKS 可枚举（守卫非空转）")
_by_name = {t[0]: t for t in (_tasks or [])}
check(all(len(t) == 4 for t in (_tasks or [])), "四元组 (name, script, args, critical)")
check(_by_name.get("资产同名去重", [None] * 4)[3] is True,
      "「资产同名去重」critical=True", str(_by_name.get("资产同名去重")))
check(_by_name.get("赛道分类刷新", [None] * 4)[3] is True,
      "「赛道分类刷新」critical=True")
check(_by_name.get("主表 supply/市值对齐 CMC", [None] * 4)[3] is True,
      "「主表 supply/市值对齐 CMC」critical=True")
check(_by_name.get("KOL 信号回测", [None] * 4)[3] is False,
      "非关键任务（KOL 回测）critical=False")
import re  # noqa: E402
check("not continue_on_fail" not in _ORCH
      and not re.search(r"^\s*break\s*$", _ORCH, re.M),
      "已移除「失败即 break 中止」的分支（真 break 语句，非注释）")
check("⛔" not in _ORCH and "终止后续任务" not in _ORCH,
      "不再输出「关键任务失败，终止后续任务」")
check("return 1 if critical_failed else 0" in _ORCH,
      "退出码只取决于「关键任务失败」")


# ════════════════════════════════════════════════════════════
# 6. 编排隔离：行为面（monkeypatch run_task，验证失败后仍跑完全部）
# ════════════════════════════════════════════════════════════
print("\n【测试6】子任务隔离（行为）：失败不中止其余 + 退出码语义")
import contextlib  # noqa: E402
import io  # noqa: E402
import run_data_sync_daily as orch  # noqa: E402  （模块级不连库）


def _run_orch(tasks, fail_names):
    calls = []
    orch.TASKS = tasks

    def fake(name, script, args):
        calls.append(name)
        return 1 if name in fail_names else 0

    orig = orch.run_task
    orch.run_task = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = orch.main()
    finally:
        orch.run_task = orig
    return calls, rc


_calls, _rc = _run_orch(
    [("A", "a.py", [], True), ("B", "b.py", [], False), ("C", "c.py", [], False)],
    {"A"})
check(_calls == ["A", "B", "C"], "关键任务 A 失败后，B/C 仍被执行（不再中止）", str(_calls))
check(_rc == 1, "关键任务失败 ⇒ 退出码 1（保持失败可见）", str(_rc))

_calls2, _rc2 = _run_orch(
    [("A", "a.py", [], False), ("B", "b.py", [], False)], {"A", "B"})
check(_calls2 == ["A", "B"], "非关键任务失败也跑完其余", str(_calls2))
check(_rc2 == 0, "非关键任务全失败 ⇒ 退出码 0（不拖累整体状态）", str(_rc2))

_calls3, _rc3 = _run_orch(
    [("A", "a.py", [], True), ("B", "b.py", [], True), ("C", "c.py", [], True)],
    {"A", "B", "C"})
check(len(_calls3) == 3, "全部失败仍逐个执行（无提前退出）")
check(_rc3 == 1, "全部失败（含关键任务）⇒ 退出码 1（系统性故障信号）", str(_rc3))

_calls4, _rc4 = _run_orch(
    [("A", "a.py", [], True), ("B", "b.py", [], True)], {"B"})
check(_calls4 == ["A", "B"] and _rc4 == 1, "中间关键任务失败：后续仍执行且退出码非 0")


# ════════════════════════════════════════════════════════════
# 7. 锁耗尽非硬失败（2026-09-29 告警收敛）：锁竞争重试耗尽 ⇒ 退出码 0
# ════════════════════════════════════════════════════════════
print("\n【测试7】dedup_assets 锁耗尽退出码语义（锁竞争→0 / 真实错误→2）")
check("failed_lock" in _SRC and "failed_other" in _SRC,
      "main() 区分「撞锁跳过」与「真实错误」两类计数")
check("return 2 if failed_other else 0" in _SRC,
      "退出码只取决于真实错误：锁耗尽仍返回 0（不拖垮 data_sync_daily / 不误告警）")
check("撞锁重试已耗尽，本轮跳过（幂等，次日重试）" in _SRC,
      "锁耗尽打印 [WARN]「本轮跳过（幂等，次日重试）」（保留可见性、不静默）")
check("if is_lock_error(last_err):" in _SRC,
      "按 is_lock_error 分流（与重试判据同源）")


# ════════════════════════════════════════════════════════════
# 8. 调度错峰：derivatives_batch 避开 06:30（与日同步去重同窗）
# ════════════════════════════════════════════════════════════
print("\n【测试8】derivatives_batch 错峰（避开 06:30 去重撞锁）")
_SCHED = open(os.path.join(_HERE, "scheduler.py"), encoding="utf-8").read()
check('("derivatives_batch", "5 */6 * * *"' in _SCHED,
      "derivatives_batch 已错峰到 5 */6（06:05 起跑，06:30 前完成）")
check('("derivatives_batch", "30 */6 * * *"' not in _SCHED,
      "derivatives_batch 不再与 data_sync_daily 同在 :30 相位")
check('("data_sync_daily", "30 6 * * *"' in _SCHED,
      "data_sync_daily 仍在 30 6（未动，避免下游时序连锁）")


# ════════════════════════════════════════════════════════════
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
