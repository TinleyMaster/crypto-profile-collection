#!/usr/bin/env python3
"""轧空通道「输出面」静默观测单测（诊断_轧空邮件断流_2026-09-23 §6.2，P0）。

运行：python test_squeeze_alert_silence.py

背景：既有看门狗只看「输入面」（数据新鲜度 / 队列 / 入队数 / 拒判数），从不看邮件是否
真的发出。影子模式把发信短路后判定照常落库 ⇒ 两层看门狗全部无感、静默断流可无限期
持续（2026-09-23 实况：04:12 后 8 笔判定 0 发信，无人告警）。

覆盖：
  1) 纯函数 `_squeeze_silence_note` 的分类（无静默 / 主动静默 / 疑似故障 / 边界）；
  2) 影子标记常量跨文件同值（daemon ↔ 看门狗，避免静默漂移）；
  3) 源码级接线守卫：daemon 影子分支写标记、看门狗调用判据且 SQL 口径正确；
  4) 文案不含 markdown 强调符（HTML 邮件禁忌）。
"""
import ast
import datetime as dt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, os.path.join(_SCRIPTS, "bin"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import check_scan_freshness as csf  # noqa: E402

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


NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)

# ════════════════════════════════════════════════════════════
# 1. 纯函数分类
# ════════════════════════════════════════════════════════════
print("\n【测试1】_squeeze_silence_note 分类")
check(csf._squeeze_silence_note(0, None, NOW) is None,
      "无静默（silenced=0）→ None")
check(csf._squeeze_silence_note(0, NOW, NOW) is None,
      "无静默（即使标记新鲜）→ None")
_fresh = csf._squeeze_silence_note(3, NOW - dt.timedelta(hours=1), NOW)
check(_fresh is not None and "影子模式" in _fresh and "主动静默" in _fresh,
      "标记新鲜（1h）→ 主动静默文案", str(_fresh))
check(_fresh is not None and "疑似" not in _fresh,
      "主动静默文案不得出现「疑似」", str(_fresh))
_stale = csf._squeeze_silence_note(3, NOW - dt.timedelta(hours=7), NOW)
check(_stale is not None and "疑似发信分支" in _stale,
      "标记陈旧（7h > 窗口 6h）→ 疑似故障文案", str(_stale))
_none = csf._squeeze_silence_note(3, None, NOW)
check(_none is not None and "疑似发信分支" in _none,
      "无标记 → 疑似故障文案", str(_none))
check("影子模式" in (csf._squeeze_silence_note(1, NOW - dt.timedelta(hours=6), NOW) or ""),
      "边界：标记恰 6h（==窗口）→ 仍判主动静默")
check("疑似" in (csf._squeeze_silence_note(
          1, NOW - dt.timedelta(seconds=6 * 3600 + 1), NOW) or ""),
      "边界：标记 6h+1s → 判疑似故障")
check(csf.SQUEEZE_SILENCE_WINDOW_H == 6, "静默观察窗 == 6h")

# ════════════════════════════════════════════════════════════
# 2. 影子标记常量跨文件同值
# ════════════════════════════════════════════════════════════
print("\n【测试2】影子标记常量跨文件同值")
import scan_daemon as sd  # noqa: E402
check(sd.SHADOW_MARKER_TASK == csf.SHADOW_MARKER_TASK == "squeeze_shadow",
      "SHADOW_MARKER_TASK 三处同值（daemon / 看门狗 / 字面量）",
      f"{sd.SHADOW_MARKER_TASK} vs {csf.SHADOW_MARKER_TASK}")

# ════════════════════════════════════════════════════════════
# 3. 源码级接线守卫
# ════════════════════════════════════════════════════════════
print("\n【测试3】源码级接线守卫")
with open(sd.__file__, encoding="utf-8") as fh:
    _sd_src = fh.read()
with open(csf.__file__, encoding="utf-8") as fh:
    _csf_src = fh.read()
_fn = next((n for n in ast.parse(_sd_src).body
            if isinstance(n, ast.FunctionDef) and n.name == "task_scan_squeeze"), None)
_sqz = ast.unparse(_fn) if _fn else ""
check("_write_heartbeat(SHADOW_MARKER_TASK, True)" in _sqz,
      "影子分支写可观测标记 _write_heartbeat(SHADOW_MARKER_TASK, True)")
check(_sqz.index("SQUEEZE_ALERT_SHADOW") < _sqz.index("_write_heartbeat(SHADOW_MARKER_TASK"),
      "标记写入在开关判断之后（非影子分支不得写标记）")
check(_sqz.index("_write_heartbeat(SHADOW_MARKER_TASK") < _sqz.index("notifier.send"),
      "标记写入在 notifier.send 之前（影子期不得触达发送）")
check("_squeeze_silence_note(" in _csf_src,
      "看门狗接线 _squeeze_silence_note(...)")
check("pool='squeeze' AND status='confirmed'" in _csf_src
      and "alerted_at IS NULL AND alert_suppressed_at IS NULL" in _csf_src,
      "输出面 SQL：pool='squeeze' + confirmed + 未发信 + 未静音")

# ════════════════════════════════════════════════════════════
# 4. 文案无 markdown 强调符（HTML 邮件禁忌）
# ════════════════════════════════════════════════════════════
print("\n【测试4】文案无 markdown 强调符")
for _n in (_fresh, _stale, _none):
    check("**" not in (_n or ""), f"文案无 **：{(_n or '')[:24]}…")

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
