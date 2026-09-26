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

# 关键 cron 白名单：(调度 key, 说明, 停滞阈值小时)。
# 阈值必须大于任务周期，否则 24h 周期任务会在调度完成后 18h（距下次调度
# 还有 6h）被误报 + 误补跑（2026-09-15 误报根因：data_sync_daily 等每日任务）。
KEY_JOBS = [
    ("catalyst_run_all", "催化剂全链路（每 12h）", 18),
    ("cmc_quote_snapshot", "CMC 行情快照（每日）", 30),
    ("data_sync_daily", "每日数据同步总调度", 30),
    ("etl_asset_market_daily", "行情快照→日级 ETL（每 6h）", 12),
    ("sync_core_supply", "主表 supply/市值对齐（每 6h）", 12),
    ("cm_incremental", "CM 链上指标 T-1 增量（每日）", 30),
    ("ingest_cryptoetf_flow", "CryptoETF 日频资金流入库（每日）", 30),
    ("market_daily", "大盘数据日频总控（恐贪/OI/CEFI/TVL/快照，每日）", 30),
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


def _last_run_error(key: str, within_seconds: float) -> str | None:
    """取该调度任务**最近 within_seconds 内**最近一次运行的 error 文本。

    用于补跑前的安全闸：上一轮若是被硬超时（`timeout:`）或卡死（`stuck:`）
    收割的，说明任务自身跑不完，补跑大概率重蹈覆辙 —— 2026-09-21 实况：
    catalyst_run_all 补跑后空转 12h 又被收割，看护再补跑，形成恶性循环。

    ⚠️ 时间窗（2026-09-26 修复）：原先取「任意历史最近一条带 error 的行」，
    一旦某天出现过 timeout/stuck，此后即使任务连续成功多日，该旧错误行仍
    一直命中（success 不改判据），补跑被永久挡住 —— 实况：data_sync_daily
    命中 18 天前的 stuck 行，导致 30.8h 停滞时未自动补跑。故只让「近阈值
    内的错误」具备拦截效力，窗口外的陈旧错误一律忽略。

    返回的 `nostart:`（取走后从未真正启动，基础设施侧静默死亡，2026-09-26 起）
    **不**计入该安全闸：调用方另行出「基础设施侧」文案，不指引排查任务侧根因。
    """
    try:
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT error FROM sys.task
                    WHERE name LIKE %s AND error IS NOT NULL
                      AND started_at > NOW() - make_interval(secs => %s)
                    ORDER BY started_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (f"[调度] {key}%", float(within_seconds)),
                )
                row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        print(f"[看护] _last_run_error 查询失败 {key}: {e}", file=sys.stderr)
        return None


def _recent_submission(key: str, within_seconds: float) -> bool:
    """该 key 在最近 within_seconds 内是否被**提交**过（pending/running/failed/done 都算）。

    2026-09-22 修复「补跑恶性循环」：看护原先只要「超阈值未 done」就补跑（仅
    timeout/stuck 两种错误才拦），于是当**任务自身**失败/卡住（scheduler 仍存活、
    照常按 cron 提交）时，看护每 18h 再补跑一次，形成「补跑→再卡→再补跑」。
    正解：近阈值内已有提交记录 ⇒ scheduler 活着、问题在任务自身 ⇒ 只告警不补跑；
    只有「近阈值内一次提交都没有」才说明 scheduler 失活、需要看护兜底补跑。
    """
    try:
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM sys.task WHERE name LIKE %s "
                    "AND started_at > NOW() - make_interval(secs => %s) LIMIT 1",
                    (f"[调度] {key}%", float(within_seconds)),
                )
                return cur.fetchone() is not None
    except Exception as e:
        print(f"[看护] _recent_submission 查询失败 {key}: {e}", file=sys.stderr)
        return False


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


def _check_key(key: str, desc: str, threshold_hours: int, check_only: bool) -> dict:
    """检查单个关键 cron：超阈值未 done → 告警 + 补跑。返回结果 dict。

    Args:
        threshold_hours: 该任务专属停滞阈值（小时），必须 > 任务周期，
            否则日频任务会在调度完成后 18h 被误报（2026-09-15 根因）。
    """
    threshold = threshold_hours * 3600
    last = _last_done_ts(key)
    now = time.time()
    if last is None:
        # 从未成功过（或表里没有）——首次发现即告警
        stale_for = None
        stale = True
    else:
        stale_for = now - last
        stale = stale_for > threshold

    if not stale:
        return {"key": key, "ok": True, "last_done": last, "stale_for": None}

    # 静默期抑制重复告警：同 key 距上次告警 < 阈值才再次发
    last_alert = _last_alerted.get(key, 0)
    if now - last_alert < threshold:
        return {"key": key, "ok": False, "stale": True, "alerted": False, "reason": "静默期内已告警"}

    _last_alerted[key] = now
    hours = round(stale_for / 3600, 1) if stale_for else 0

    # 补跑安全闸：**近阈值内**上一轮被硬超时/卡死收割 ⇒ 任务自身跑不完，
    # 补跑只会再空转一轮（窗口外陈旧错误不拦，否则一次 stuck 永久阻塞补跑）
    last_err = _last_run_error(key, threshold) or ""
    blocked_by = last_err if last_err.startswith(("timeout:", "stuck:")) else None
    # 静默死亡（`nostart:`：被取走后从未真正启动）多因 DB 写不可达等**基础设施侧**
    # 原因，非任务自身跑不完 ⇒ 单独出文案，不再指引去查「LLM 欠费 / 上游限频」
    # （2026-09-26）。它不在 blocked_by 内；`last_err` 非空 ⇒ 该行必落在同一
    # recent 窗口内 ⇒ 补跑决策与改前一致（不新增自动补跑）。
    infra_died = last_err if last_err.startswith("nostart:") else None
    # 近阈值内已有提交（scheduler 存活）⇒ 只告警不补跑，避免补跑恶性循环
    recent = _recent_submission(key, threshold)

    # 告警邮件
    subject = f"⚠️ [看护] 调度任务 {key} 停滞 {hours}h"
    if blocked_by:
        rerun_line = (f"⚠️ 未自动补跑：上一轮判定为「{blocked_by[:100]}」，"
                      f"补跑大概率重蹈覆辙，请先排查根因（如 LLM 欠费 / 上游限频）。")
    elif infra_died:
        rerun_line = (f"⚠️ 未自动补跑：上一轮「{infra_died[:120]}」。"
                      f"此为**基础设施侧**（DB 写不可达等），非任务自身跑不完 —— "
                      f"近 {threshold_hours}h 内已有提交记录，下一轮 cron 会自然重试；"
                      f"若持续出现请排查 DB 连通性与平台事件（勿按任务侧根因排查）。")
    elif recent:
        rerun_line = (f"⚠️ 未自动补跑：近 {threshold_hours}h 内已有提交记录（scheduler 存活），"
                      f"问题在任务自身（失败/卡住），非调度失活。"
                      f"最近错误：{(last_err or '无')[:160]}")
    else:
        rerun_line = f"如 scheduler 进程失活，将自动补跑 {key}。"
    body = (
        f"关键 cron「{key}」({desc}) 已 {hours} 小时无成功执行。\n"
        f"最近一次 done: {last or '从未成功'}\n"
        f"阈值: {threshold_hours}h\n\n"
        f"{rerun_line}"
    )
    mail_ok = _send_alert_email(subject, body)

    # 补跑（check_only 时不补；上一轮 timeout/stuck 或近阈值内已提交时也不补）
    task_id = None
    if not check_only and not blocked_by and not recent:
        for _key, _cron, script, a, d, cat in __import__("scheduler").SCHEDULE:
            if _key == key:
                task_id = submit_scheduled_task(key, script, a, d, category=cat)
                break

    return {
        "key": key, "ok": False, "stale": True, "alerted": True,
        "stale_for": stale_for, "mail_ok": mail_ok, "rerun_task_id": task_id,
        "rerun_blocked_by": blocked_by, "recent_submission": recent,
        "reason": "超阈值未成功，已告警" + (
            "（补跑已阻止：上轮 " + blocked_by.split(":")[0] + "）" if blocked_by
            else ("（补跑已阻止：上轮静默死亡，基础设施侧）" if infra_died
                  else ("（补跑已阻止：近阈值内有提交，疑似任务自身问题）" if recent
                        else ("并补跑" if task_id else "（补跑未触发）")))),
    }


def run_once(check_only: bool = False) -> int:
    results = []
    for key, desc, threshold_hours in KEY_JOBS:
        r = _check_key(key, desc, threshold_hours, check_only)
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
