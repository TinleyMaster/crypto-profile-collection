#!/usr/bin/env python3
"""投研页 unlock_pct_30d 恒 0.0 修复探针（审计_投研页机会挖掘_8680_TAKE_2026-09-24.md P1）。

运行：python workbench/test_unlock_pct_30d.py（纯离线）

判据：
  1. `_parse_unlock_event_date` 能解析人类可读日期（"Sep 25, 2026" / "25 Dec 2026" /
     "Sep 12, 2026Next"）与 ISO 日期；
  2. `_build_structured_metrics_inner` 的 `unlock.unlock_pct_30d` 会累加 30 天内
     upcoming 事件的 pct，不再因 `datetime.fromisoformat` 解析失败而塌成 0.0；
  3. 源码级护栏：该路径调用 `_parse_unlock_event_date`，且不再出现
     `datetime.fromisoformat`。
"""
import ast
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from db_stats import (  # noqa: E402
    _build_structured_metrics_inner,
    _parse_unlock_event_date,
)

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


def _upcoming_date(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%b %d, %Y")


print("\n【1】_parse_unlock_event_date 多格式")
check(_parse_unlock_event_date("Sep 25, 2026").isoformat() == "2026-09-25",
      "human 'Sep 25, 2026'", f"got={_parse_unlock_event_date('Sep 25, 2026')}")
check(_parse_unlock_event_date("25 Dec 2026").isoformat() == "2026-12-25",
      "human '25 Dec 2026'", f"got={_parse_unlock_event_date('25 Dec 2026')}")
check(_parse_unlock_event_date("Sep 12, 2026Next").isoformat() == "2026-09-12",
      "human 'Sep 12, 2026Next'", f"got={_parse_unlock_event_date('Sep 12, 2026Next')}")
check(_parse_unlock_event_date("2026-09-25").isoformat() == "2026-09-25",
      "ISO '2026-09-25'", f"got={_parse_unlock_event_date('2026-09-25')}")
check(_parse_unlock_event_date(None) is None, "None → None")
check(_parse_unlock_event_date("") is None, "空串 → None")
check(_parse_unlock_event_date("not a date") is None, "垃圾串 → None")

print("\n【2】unlock_pct_30d 累加 30 天内事件（核心回归）")
snapshot = {
    "structured": {
        "unlocks": {
            # input_snapshot 提供 market，避免测试触发 CMC 快照 DB 查询
            "input_snapshot": {"price": 1.23, "market_cap": 1_000_000},
            "events": [
                {"is_upcoming": True, "date": _upcoming_date(1), "pct": 5.6},
                {"is_upcoming": True, "date": _upcoming_date(10), "pct": 3.3},
                {"is_upcoming": True, "date": _upcoming_date(60), "pct": 4.2},
                {"is_upcoming": False, "date": _upcoming_date(2), "pct": 99.0},
            ],
        }
    }
}
metrics = _build_structured_metrics_inner(snapshot, 8680)
u = metrics.get("unlock", {})
check(abs(u.get("unlock_pct_30d", -1) - 8.9) < 1e-9,
      "30 天内 5.6+3.3=8.9（60 天与已过事件不计）", f"got={u.get('unlock_pct_30d')}")
check(u.get("upcoming_events_count") == 3,
      "upcoming_events_count=3", f"got={u.get('upcoming_events_count')}")
check(u.get("data_available") is True, "data_available=True")
check(u.get("next_unlock_pct") == 5.6, "next_unlock_pct 直取首个", f"got={u.get('next_unlock_pct')}")

print("\n【3】人类可读日期不再塌成 0.0（审计原例）")
snap8680 = {
    "structured": {
        "unlocks": {
            "input_snapshot": {"price": 1.0},
            "events": [{"is_upcoming": True, "date": "Sep 25, 2026", "pct": 5.6}],
        }
    }
}
m8680 = _build_structured_metrics_inner(snap8680, 8680)
check(m8680["unlock"]["unlock_pct_30d"] == 5.6,
      "'Sep 25, 2026' 5.6 → unlock_pct_30d=5.6（原 bug 恒 0.0）",
      f"got={m8680['unlock']['unlock_pct_30d']}")

print("\n【4】无 parse 失败时保持 0.0（真无 30 天内解锁）")
snap_none = {
    "structured": {
        "unlocks": {
            "input_snapshot": {"price": 1.0},
            "events": [{"is_upcoming": True, "date": _upcoming_date(90), "pct": 9.9}],
        }
    }
}
check(_build_structured_metrics_inner(snap_none, 1)["unlock"]["unlock_pct_30d"] == 0.0,
      "仅 90 天外事件 → 0.0")

print("\n【5】源码护栏：调用多格式解析器，不含 datetime.fromisoformat")
src = open(os.path.join(_HERE, "db_stats.py"), encoding="utf-8").read()
tree = ast.parse(src)
inner = next((n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_structured_metrics_inner"),
             None)
check(inner is not None, "定位 _build_structured_metrics_inner")
if inner is not None:
    calls = [n.func.id for n in ast.walk(inner)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    check("_parse_unlock_event_date" in calls,
          "内部调用 _parse_unlock_event_date", f"calls={sorted(set(calls))}")
    attrs = [n.attr for n in ast.walk(inner)
             if isinstance(n, ast.Attribute) and n.attr == "fromisoformat"]
    check("fromisoformat" not in attrs,
          "内部不再出现 fromisoformat", f"attrs={attrs}")

print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
