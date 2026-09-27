#!/usr/bin/env python3
"""链上快照新鲜度修复（工单 OBI-OPT-SNAPSHOT-FRESHNESS X3）· 离线回归护栏。

运行：python workbench/test_snapshot_freshness_20260927.py（纯离线，不连网、不连库）

覆盖：
  A. scheduler.py —— 四条 `chain_holder_snapshot_*` 由隔日改每日（day-of-week=*）+ 错峰；
     单轮 `--limit` 保留（防无界「跑到完」饿死 chain 槽位）
  B. scheduler.py —— `onchain_snapshot_freshness` 已注册（每小时、monitor 类）
  C. check_onchain_snapshot_freshness.py —— 滞后判定（含 2 天边界 / 表空不静默）+ 去重键
  D. phase_chain_holder_batch.py —— `get_pending_assets` 改「最久未采优先」且保留 LIMIT
  E. phase_chain_holder_snapshot_auto.py —— 已标 DEPRECATED，但 app.py 手动入口与文件均保留
"""
import os
import re
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, _BIN)

_SCHED = open(os.path.join(_HERE, "scheduler.py"), encoding="utf-8").read()
_APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
_BATCH = open(os.path.join(_BIN, "phase_chain_holder_batch.py"), encoding="utf-8").read()
_AUTO = open(os.path.join(_BIN, "phase_chain_holder_snapshot_auto.py"), encoding="utf-8").read()

import check_onchain_snapshot_freshness as wd  # noqa: E402

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


# ── A. scheduler 四条链上快照：每日 + 错峰 + 保 limit ──
print("[A] scheduler chain_holder_snapshot_*")
_lines = [ln for ln in _SCHED.splitlines() if '"chain_holder_snapshot_' in ln]
check(len(_lines) == 4, "A1 四条链上快照调度存在", f"got {len(_lines)}")

_crons = {}
for ln in _lines:
    m = re.search(r'"chain_holder_snapshot_(\w+)",\s*"([^"]+)"', ln)
    if m:
        _crons[m.group(1)] = m.group(2)
check(set(_crons) == {"bsc", "eth", "base_arb", "solana"},
      "A2 四链齐备（bsc/eth/base_arb/solana）", str(sorted(_crons)))

for key, cron in _crons.items():
    f = cron.split()
    check(len(f) == 5 and f[4] == "*",
          f"A3 {key} 改为每日运行（day-of-week=*）", cron)
check(not any(x in ln for ln in _lines for x in ("1,3,5", "2,4,6")),
      "A4 隔日 cron（1,3,5 / 2,4,6）已清除")
check(len({tuple(c.split()[:2]) for c in _crons.values()}) == len(_crons),
      "A5 四链错峰（时/分组合各不相同）", str(_crons))
check(all('"--limit"' in ln for ln in _lines),
      "A6 单轮 --limit 保留（防 chain 槽位饿死，未改无界「跑到完」）")

# ── B. onchain 看门狗注册 ──
print("[B] scheduler onchain_snapshot_freshness")
check('"onchain_snapshot_freshness", "20 * * * *"' in _SCHED,
      "B1 已注册，每小时（20 * * * *）")
check('"check_onchain_snapshot_freshness.py"' in _SCHED, "B2 指向新脚本")
check(re.search(r'"onchain_snapshot_freshness"[\s\S]{0,300}?"monitor"', _SCHED) is not None,
      "B3 归为 monitor 类")

# ── C. 看门狗滞后判定（注入假连接，纯逻辑）──
print("[C] check_onchain_snapshot_freshness 判定")


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, *a, **k):
        return _Cur(self._rows)


_today = datetime.now(wd.BJ).date()
_items = wd._collect_items(_Conn([
    {"chain": "bsc", "mx": _today},
    {"chain": "eth", "mx": _today - timedelta(days=3)},
]))
_by = {i["chain"]: i for i in _items}
check(_by["bsc"]["stale"] is False and _by["bsc"]["age_days"] == 0,
      "C1 今日快照判新鲜", str(_by["bsc"]))
check(_by["eth"]["stale"] is True and _by["eth"]["age_days"] == 3,
      "C2 滞后 3 天判告警（阈值 2）", str(_by["eth"]))
check(wd._collect_items(_Conn([{"chain": "x", "mx": _today - timedelta(days=2)}]))[0]["stale"] is False,
      "C3 恰 2 天不告警（>2 才告警，边界）")
_empty = wd._collect_items(_Conn([]))
check(len(_empty) == 1 and _empty[0]["stale"] is True and _empty[0]["mx"] is None,
      "C4 整表为空按滞后告警（不静默通过）", str(_empty))
check(wd.MAX_STALE_DAYS == 2 and wd.ALERT_TASK_KEY == "onchain_snapshot_stall",
      "C5 阈值 2 天 / 独立去重键 onchain_snapshot_stall")
_wd_src = open(os.path.join(_BIN, "check_onchain_snapshot_freshness.py"), encoding="utf-8").read()
check("biz.scan_stall_alert" in _wd_src and "ON CONFLICT (task) DO UPDATE" in _wd_src,
      "C6 复用 scan_stall_alert 去重表（upsert）")
check("last_email_ts=NULL" in _wd_src, "C7 恢复时清空告警时间戳")

# ── D. batch 旋转排序 ──
print("[D] phase_chain_holder_batch 排序")
check("ORDER BY ls.last_dt ASC NULLS FIRST" in _BATCH,
      "D1 改为「最久未采优先」排序（NULLS FIRST 让未采过的恒排最前）")
check("LEFT JOIN LATERAL" in _BATCH and "MAX(s.snapshot_date) AS last_dt" in _BATCH,
      "D2 取每资产最近一次快照日")
check("LIMIT %s" in _BATCH, "D3 单轮 LIMIT 保留")
check("NOT EXISTS (" in _BATCH and "AT TIME ZONE 'Asia/Shanghai'" in _BATCH,
      "D4 今日已采排除（北京时间口径）仍保留")

# ── E. 悬空文件处置 ──
print("[E] phase_chain_holder_snapshot_auto 处置")
check("DEPRECATED" in _AUTO, "E1 文件头已标 DEPRECATED")
check(os.path.exists(os.path.join(_BIN, "phase_chain_holder_snapshot_auto.py")),
      "E2 文件未删除（保留 app.py 手动入口）")
check("chain_holder_snapshot_auto" in _APP and "phase_chain_holder_snapshot_auto.py" in _APP,
      "E3 app.py 手动触发条目仍指向该文件（未悬空）")
check("phase_chain_holder_snapshot_auto" not in _SCHED,
      "E4 该文件不被 scheduler 调度（自动化角色确已废弃）")

print("=" * 60)
print(f"结果：{passed} 通过 / {failed} 失败")
print("=" * 60)
sys.exit(1 if failed else 0)