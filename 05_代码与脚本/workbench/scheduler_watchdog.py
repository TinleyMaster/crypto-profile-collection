#!/usr/bin/env python3
"""调度健康看护（scheduler_watchdog）——独立轻量常驻进程，由 supervisord 托管。

职责（H3，BUG-SCHED-PROC-001）：
1. 每 N 分钟轮询 sys.task，对关键 cron（白名单）查最近一次 done 的 ended_at。
2. 若 now - last_done > 阈值（默认 18h，catalyst 每 12h 留 6h 余量）→ 发告警邮件 + 自动补跑。
3. 心跳：把自身存活状态写 sys.task（name=[看护] scheduler_watchdog），
   供诊断确认看护进程在跑（验收项 4 之一）。

用法：
    python scheduler_watchdog.py                # 前台常驻（supervisord 托管）
    python scheduler_watchdog.py --once         # 单次检查（测试/手动）
    python scheduler_watchdog.py --check-only   # 只检查不发不补（dry run）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

# 复用 scheduler.py 的告警/补跑/DB 工具
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

from scheduler import _send_alert_email, submit_scheduled_task  # noqa: E402

# 把 scripts/src 加入 path（crypto_research 包）
if os.path.exists("/app/scripts/src"):
    _SCRIPTS_SRC = Path("/app/scripts/src")
else:
    _SCRIPTS_SRC = _here.parent.parent / "scripts" / "src"
if str(_SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_SRC))

from task_manager import _get_db, _insert_task, _append_log, LOG_DIR, STATE_FILE  # noqa: E402

# ── 配置（可用环境变量覆盖） ──
POLL_SECONDS = int(os.getenv("WATCHDOG_POLL_SECONDS", "1800"))     # 默认每 30 分钟
STALE_THRESHOLD_SECONDS = int(os.getenv("WATCHDOG_STALE_SECONDS", "64800"))  # 默认 18h

# 关键 cron 白名单：(调度 key, 说明)。超过阈值未 done 则告警+补跑。
# 覆盖系统性 cron：catalyst 每 12h、data_sync 每日、cmc 每日、etl/sync 每 6h。
KEY_JOBS = [
    ("catalyst_run_all", "催化剂全链路（每 12h）"),
    ("cmc_quote_snapshot", "CMC 行情快照（每日）"),
    ("data_sync_daily", "每日数据同步总调度"),
    ("etl_asset_market_daily", "行情快照→日级 ETL（每 6h）"),
    ("sync_core_supply", "主表 supply/市值对齐（每 6h）"),
    ("cm_incremental", "CM 链上指标 T-1 增量（每日）"),
]

# 告警状态：每 key 记录上次告警时间，避免重复轰炸
_last_alerted: dict[str, float] = {}


def _last_done_ts(key: str) -> float | None:
    """取该调度任务最近一次 done 的 ended_at（epoch 秒）。"""
    try:
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ended_at FROM sys.task
                    WHERE name LIKE %s
                      AND status = 'done'
                      AND ended_at IS NOT NULL
                    ORDER BY ended_at DESC
                    LIMIT 1
                    """,
                    (f"[调度] {key}%",),
                )
                row = cur.fetchone()
        if row and row[0]:
            return row[0].timestamp()
        return None
    except Exception as e:
        print(f"[看护] _last_done_ts 查询失败 {key}: {e}", file=sys.stderr)
        return None


def _write_heartbeat() -> None:
    """写心跳任务（name=[看护] scheduler_watchdog，status=running，极短存活）。"""
    try:
        task_id = uuid.uuid4().hex[:12]
        task = {
            "task_id": task_id,
            "name": "[看护] scheduler_watchdog",
            "status": "running",
            "cmd": ["python", "scheduler_watchdog.py"],
            "started_at": time.time(),
            "ended_at": time.time(),
            "stats": {"scheduler_key": "scheduler_watchdog"},
            "error": None,
        }
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sys.task (
                        task_id, name, status, cmd, started_at, ended_at, stats, error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (task_id, task["name"], "done", task["cmd"],
                     _dt(task["started_at"]), _dt(task["ended_at"]),
                     __import__("json").dumps(task["stats"]), None),
                )
    except Exception as e:
        print(f"[看护] 心跳写库失败: {e}", file=sys.stderr)


def _dt(ts: float):
    """epoch 秒 → datetime（写库用）。"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _check_key(key: str, desc: str, check_only: bool) -> dict:
    """检查单个关键 cron：超阈值未 done → 告警 + 补跑。返回结果 dict。"""
    last = _last_done_ts(key)
    now = time.time()
    if last is None:
        # 从未成功过（或表里没有）——首次发现即告警
        stale_for = None
        stale = True
    else:
        stale_for = now - last
        stale = stale_for > STALE_THRESHOLD_SECONDS

    if not stale:
        return {"key": key, "ok": True, "last_done": last, "stale_for": None}

    # 静默期抑制重复告警：同 key 距上次告警 < 阈值才再次发
    last_alert = _last_alerted.get(key, 0)
    if now - last_alert < STALE_THRESHOLD_SECONDS:
        return {"key": key, "ok": False, "stale": True, "alerted": False, "reason": "静默期内已告警"}

    _last_alerted[key] = now
    hours = round(stale_for / 3600, 1) if stale_for else 0

    # 告警邮件
    subject = f"⚠️ [看护] 调度任务 {key} 停滞 {hours}h"
    body = (
        f"关键 cron「{key}」({desc}) 已 {hours} 小时无成功执行。\n"
        f"最近一次 done: {last or '从未成功'}\n"
        f"阈值: {STALE_THRESHOLD_SECONDS // 3600}h\n\n"
        f"如 scheduler 进程失活，将自动补跑 {key}。"
    )
    mail_ok = _send_alert_email(subject, body)

    # 补跑（check_only 时不补）
    task_id = None
    if not check_only:
        for _key, _cron, script, a, d in __import__("scheduler").SCHEDULE:
            if _key == key:
                task_id = submit_scheduled_task(key, script, a, d)
                break

    return {
        "key": key, "ok": False, "stale": True, "alerted": True,
        "stale_for": stale_for, "mail_ok": mail_ok, "rerun_task_id": task_id,
        "reason": "超阈值未成功，已告警" + ("并补跑" if task_id else "（补跑未触发）"),
    }


def run_once(check_only: bool = False) -> int:
    results = []
    for key, desc in KEY_JOBS:
        r = _check_key(key, desc, check_only)
        results.append(r)
        if not r["ok"]:
            print(
                f"[看护] {key}: {r.get('reason')}"
                + (f" 补跑 task={r.get('rerun_task_id')}" if r.get("rerun_task_id") else "")
            )
        else:
            print(f"[看护] {key}: 正常")
    _write_heartbeat()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="调度健康看护")
    parser.add_argument("--once", action="store_true", help="单次检查后退出")
    parser.add_argument("--check-only", action="store_true", help="只检查，不发告警不补跑")
    args = parser.parse_args()

    print(f"[看护] 启动，轮询每 {POLL_SECONDS}s，停滞阈值 {STALE_THRESHOLD_SECONDS // 3600}h")
    print(f"[看护] 状态文件: {STATE_FILE} | 日志目录: {LOG_DIR}")

    if args.once or args.check_only:
        return run_once(check_only=args.check_only)

    # 常驻循环
    while True:
        try:
            run_once(check_only=False)
        except Exception as e:
            print(f"[看护] 循环异常: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())