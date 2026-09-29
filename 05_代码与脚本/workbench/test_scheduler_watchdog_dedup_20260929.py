#!/usr/bin/env python3
"""scheduler_watchdog 告警去重持久化护栏（2026-09-29）。

问题：看护原用进程内 `_last_alerted` dict 做去重，**每次容器重启即清零** ⇒
频繁 redeploy 期间同一停滞状态被反复告警（实测同一 stale 在 30.7h / 32.5h
两次发出）。修法：持久化到 `biz.scan_stall_alert`（key 前缀 `sched_stall:`）。

运行：python workbench/test_scheduler_watchdog_dedup_20260929.py
      （纯离线：源码断言 + 假连接，不连库、不发信）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_SRC = open(os.path.join(_HERE, "scheduler_watchdog.py"), encoding="utf-8").read()

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


# ── 源码断言 ──
print("[源码] 去重已持久化")
check('ALERT_DEDUP_PREFIX = "sched_stall:"' in _SRC, "去重 key 前缀 sched_stall:")
check("biz.scan_stall_alert" in _SRC, "复用 biz.scan_stall_alert 表")
check("_last_alert_ts(key)" in _SRC and "now - last_alert < threshold" in _SRC,
      "_check_key 用 _last_alert_ts 判静默期")
check("_last_alerted.get(" not in _SRC, "不再用进程内 dict 判静默（重启会清零）")
check("if mail_ok:" in _SRC and "_mark_alerted(key)" in _SRC,
      "仅发送成功后落去重时间（失败不占位）")

# ── 行为断言（假连接） ──
print("[行为] _last_alert_ts / _mark_alerted")
try:
    import scheduler_watchdog as sw  # noqa: E402

    class _Cur:
        def __init__(self, row=None):
            self._row = row
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return self._row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, cur):
            self._cur = cur

        def cursor(self, *a, **k):
            return self._cur

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # _last_alert_ts：命中已存时间
    _ts = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
    _cur = _Cur(row=(_ts,))
    _orig = sw._get_db
    sw._get_db = lambda: _Conn(_cur)
    try:
        _got = sw._last_alert_ts("data_sync_daily")
        check(abs(_got - _ts.timestamp()) < 1, "_last_alert_ts 解析 last_email_ts", str(_got))
        _sql, _params = _cur.calls[-1]
        check("biz.scan_stall_alert" in _sql and _params == ("sched_stall:data_sync_daily",),
              "_last_alert_ts 查询 key 加前缀 sched_stall:", str(_params))

        # _last_alert_ts：无记录 → None
        _cur2 = _Cur(row=None)
        sw._get_db = lambda: _Conn(_cur2)
        check(sw._last_alert_ts("x") is None, "无记录 → None")

        # _mark_alerted：UPSERT
        _cur3 = _Cur()
        sw._get_db = lambda: _Conn(_cur3)
        sw._mark_alerted("data_sync_daily")
        _sql3, _p3 = _cur3.calls[-1]
        check("INSERT INTO biz.scan_stall_alert" in _sql3
              and "ON CONFLICT (task) DO UPDATE" in _sql3 and _p3 == ("sched_stall:data_sync_daily",),
              "_mark_alerted UPSERT 到 scan_stall_alert（带前缀 key）", _sql3[:80])
    finally:
        sw._get_db = _orig
except Exception as e:
    check(False, "scheduler_watchdog 可导入并测 helpers", f"{type(e).__name__}: {e}")

print("\n" + "=" * 46)
print(f"{passed}/{passed + failed} 通过")
print("=" * 46)
sys.exit(1 if failed else 0)
