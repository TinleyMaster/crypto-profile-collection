#!/usr/bin/env python3
"""催化剂 A 级「快讯 × 慢通道 digest」跨通道去重 —— 离线护栏测试。

来源：`诊断_催化剂A级邮件延迟链路_XRP_BCH_2026-09-24.md`（顺带发现）
现象：同一 A 级信号被两个通道各发一封（实测 XRP signal=1085989：
      16:30 slow_digest「🎯 催化剂 Alert·A级 2 条」+ 16:50 fast_alert
      「🚀 A级催化剂信号: XRP」）。两通道各用独立去重（快讯按
      `(signal_id, fast_alert)`；digest 按类别 sentinel）⇒ 无跨通道合围。

修复：`notified_at`（仅 digest 发送成功后写）与 `pre_alert_sent_at`
      （仅快讯发送成功后写）互为「另一通道已覆盖」的判据，双向排除。

覆盖：
  1. `_slow_digest_sent_recently` 纯行为（命中/空/异常兜底/空入参不查库）
  2. `_recent_new_a_signals` SQL 含 `pre_alert_sent_at` 排除 + 参数顺序
  3. `send_fast_alerts_for_new_signals` 含跨通道检查，且仍在取锁之前
  4. 既有不变量不回归：取锁调用仍先于 AI 否决判定

运行: python test_catalyst_channel_dedup.py
"""
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from catalyst import notifier as N  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"    {detail}")


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    """最小桩：记录 execute 调用，返回预设行。"""

    def __init__(self, rows=None, raise_on_execute=False):
        self._rows = rows or []
        self._raise = raise_on_execute
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._raise:
            raise RuntimeError("boom")
        return _Cur(self._rows)


_SRC = open(N.__file__, encoding="utf-8").read()
_m = re.search(r"def send_fast_alerts_for_new_signals\(.*?\n(.*?)\ndef ", _SRC, re.S)
_SEND_SRC = _m.group(1) if _m else ""


# ════════════════════════════════════════════════════════════
print("\n【测试1】_slow_digest_sent_recently 行为")
# ════════════════════════════════════════════════════════════

fn = N._slow_digest_sent_recently

_c = _Conn([{"signal_id": 5}, {"signal_id": 7}])
got = fn(_c, [5, 7, 9])
check(got == {5, 7}, "命中近窗口的 signal_id 集合", f"got={got}")
check(len(_c.calls) == 1, "只发一次查询")
_sql, _params = _c.calls[0]
check("notified_at > NOW()" in _sql, "以 notified_at 近窗口为判据")
check(_params == ([5, 7, 9], N.DEDUP_WINDOW_HOURS), "参数 = (ids, DEDUP_WINDOW_HOURS)", f"got={_params}")

_c2 = _Conn([])
check(fn(_c2, []) == set(), "空入参 → 空集")
check(len(_c2.calls) == 0, "空入参不查库")
check(fn(_Conn([]), [None, 0, -3]) == set(), "全为无效 id → 空集")

_c3 = _Conn(raise_on_execute=True)
check(fn(_c3, [5]) == set(), "查询异常 → 兜底空集（按未发送处理，不阻断发信）")

# ════════════════════════════════════════════════════════════
print("\n【测试2】_recent_new_a_signals 排除近窗口已发快讯的行")
# ════════════════════════════════════════════════════════════

_c4 = _Conn([])
N._recent_new_a_signals(_c4, hours=24, asset_class="crypto")
_sql4, _params4 = _c4.calls[0]
check("pre_alert_sent_at IS NULL" in _sql4, "SQL 含 pre_alert_sent_at 空值分支")
check("s.pre_alert_sent_at < NOW()" in _sql4, "SQL 含 pre_alert_sent_at 过期分支")
check(_params4 == (24, N.DEDUP_WINDOW_HOURS),
      "参数顺序 = (hours, DEDUP_WINDOW_HOURS)", f"got={_params4}")
# 既有语义未动
check("s.status = 'open'" in _sql4 and "s.tier = 'A'" in _sql4, "A 级 + open 入选口径不变")
check("s.entry_price IS NOT NULL" in _sql4, "档位齐全闸门不变")

# ════════════════════════════════════════════════════════════
print("\n【测试3】快讯侧跨通道检查 + 结构位次")
# ════════════════════════════════════════════════════════════

check(bool(_SEND_SRC), "成功抽出发送函数体")
check("_slow_digest_sent_recently(" in _SEND_SRC, "函数体内调用跨通道去重")
check('if row["signal_id"] in _slow_sent:' in _SEND_SRC, "循环内按 signal_id 跳过 digest 已发信号")

_i_slow = _SEND_SRC.find("_slow_digest_sent_recently(")
_i_lock = _SEND_SRC.find("_try_acquire_send_lock(")
_i_skip = _SEND_SRC.find('if row["signal_id"] in _slow_sent:')
check(0 <= _i_slow < _i_lock,
      "跨通道查询先于取锁（不为注定跳过的行留 sending 残迹）",
      f"slow@{_i_slow} lock@{_i_lock}")
check(0 <= _i_skip < _i_lock, "跳过判定先于取锁", f"skip@{_i_skip} lock@{_i_lock}")

# ════════════════════════════════════════════════════════════
print("\n【测试4】既有不变量不回归")
# ════════════════════════════════════════════════════════════

_i_veto = _SEND_SRC.find("_ai_review_blocks_alert(")
check(_i_lock >= 0 and _i_veto >= 0, "取锁与否决判定调用点仍在")
check(_i_lock < _i_veto, "取锁仍先于 AI 否决判定（历史 sent 不被改写）",
      f"lock@{_i_lock} veto@{_i_veto}")
check('"suppressed": suppressed,' in _SEND_SRC, "返回契约含 suppressed 不变")
check('"skipped": skipped,' in _SEND_SRC or '"skipped": len(new_signal_ids)' in _SEND_SRC,
      "返回契约含 skipped 不变")

print("\n" + "=" * 50)
print(f"通过 {passed} / 失败 {failed}")
print("=" * 50)
sys.exit(1 if failed else 0)
