#!/usr/bin/env python3
"""调度健康看护（scheduler_watchdog）——独立轻量常驻进程，由 supervisord 托管。

职责：
1. 每 N 分钟轮询 sys.task，对关键 cron（白名单）查最近一次 done 的 ended_at。
2. 若 now - last_done > 任务专属阈值 → 发告警邮件 + 自动补跑。
3. 失败检测：最近一次执行若为 fail 状态，立即告警（防静默失败）。
4. 数据完整性校验：对有落库的关键任务（如 market_daily），校验产出数据量。
5. 每日健康日报：每天 09:00 发送一份总览，让你一眼知道哪些任务健康哪些有问题。
6. 心跳：把自身存活状态写 sys.task（name=[看护] scheduler_watchdog）。

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
from datetime import datetime, timezone, timedelta
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

# 关键 cron 白名单：(调度 key, 说明, 停滞阈值秒, 频率标签, 数据完整性校验函数名或 None)
# 阈值设计：频率周期 × 1.3 + 缓冲，避免正常抖动误报
#   - 每日任务：24h + 4h = 28h = 100800s
#   - 每 12h：12h + 4h = 16h = 57600s
#   - 每 6h：6h + 3h = 9h = 32400s
#   - 每 30min（监控类）：1h = 3600s
KEY_JOBS = [
    # ═══ 核心日频（28h 阈值）═══
    ("cmc_pipeline", "CMC 一键流水线（每日）", 100800, "daily", None),
    ("dl_pipeline", "DefiLlama 一键流水线（每日）", 100800, "daily", None),
    ("market_daily", "大盘数据日频总控（采集+快照）", 100800, "daily", "check_market_daily_integrity"),
    ("data_sync_daily", "每日数据同步总调度", 100800, "daily", None),
    ("cmc_quote_snapshot", "CMC 行情快照（每日3次）", 32400, "6h", None),  # 每天3次，按6h算
    ("cmc_macro_daily", "CMC 宏观三指标（每日）", 100800, "daily", None),
    ("cmc_snapshot_gap_check", "CMC 快照缺口自检", 100800, "daily", None),

    # ═══ 每 6h 级（9h 阈值）═══
    ("etl_asset_market_daily", "行情快照→日级 ETL", 32400, "6h", None),
    ("sync_core_supply", "主表 supply/市值对齐", 32400, "6h", None),
    ("derivatives_batch", "衍生品资金面批量采集", 32400, "6h", None),

    # ═══ 每 12h 级（16h 阈值）═══
    ("catalyst_run_all", "催化剂全链路", 57600, "12h", None),

    # ═══ 链上日频（28h 阈值）═══
    ("cm_incremental", "CM 链上指标 T-1 增量", 100800, "daily", None),
    ("cm_obm_ingest", "OBM BTC 链上指标入库", 100800, "daily", None),
    ("long_tail_screen_daily", "长尾初筛", 100800, "daily", None),
    ("meme_risk_daily", "Meme 五维风险标签", 100800, "daily", None),
    ("lifecycle_daily", "Meme 四阶段生命周期", 100800, "daily", None),

    # ═══ 监控类（1h 阈值，短周期任务）═══
    ("binance_bapi_health", "Binance bapi 存活探测", 86400, "daily", None),  # 每天2次，按日算
    ("watchlist_monitor", "解锁/空头/大户监控", 7200, "2h", None),  # 每30min，放宽到2h
]

# 健康日报发送时间（本地时区小时），每天只发一次
DAILY_REPORT_HOUR = int(os.getenv("WATCHDOG_REPORT_HOUR", "9"))

# 告警静默期：同 key 距上次告警至少间隔（避免轰炸）
ALERT_COOLDOWN_SECONDS = int(os.getenv("WATCHDOG_COOLDOWN", "21600"))  # 默认 6h

# 告警状态：每 key 记录上次告警时间，避免重复轰炸
_last_alerted: dict[str, float] = {}
_last_fail_alerted: dict[str, float] = {}
_last_daily_report_date: str | None = None


def _last_task_record(key: str) -> dict | None:
    """取该调度任务最近一次执行记录（无论成功失败）。"""
    try:
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT name, status, started_at, ended_at, error, stats
                    FROM sys.task
                    WHERE name LIKE %s
                    ORDER BY COALESCE(ended_at, started_at) DESC
                    LIMIT 1
                    """,
                    (f"[调度] {key}%",),
                )
                row = cur.fetchone()
        if row:
            return {
                "name": row[0],
                "status": row[1],
                "started_at": row[2],
                "ended_at": row[3],
                "error": row[4],
                "stats": row[5] or {},
            }
        return None
    except Exception as e:
        print(f"[看护] _last_task_record 查询失败 {key}: {e}", file=sys.stderr)
        return None


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
    """写心跳任务（name=[看护] scheduler_watchdog，status=done）。"""
    try:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sys.task (
                        task_id, name, status, cmd, started_at, ended_at, stats, error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (task_id, "[看护] scheduler_watchdog", "done",
                     ["python", "scheduler_watchdog.py"],
                     _dt(now), _dt(now),
                     __import__("json").dumps({"scheduler_key": "scheduler_watchdog"}),
                     None),
                )
    except Exception as e:
        print(f"[看护] 心跳写库失败: {e}", file=sys.stderr)


def _dt(ts: float):
    """epoch 秒 → datetime（写库用）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ══════════════════════════════════════════════════════════════
# 数据完整性校验函数
# ══════════════════════════════════════════════════════════════

def check_market_daily_integrity() -> tuple[bool, str]:
    """校验 market_daily 产出完整性：
    - 今日 biz.category_tvl_daily 赛道数 >= 50
    - 今日 biz.market_snapshot_daily 有记录
    - 今日 biz.btc_oi_daily 有记录
    返回 (是否健康, 详情文本)
    """
    import json
    problems = []

    try:
        with _get_db() as conn:
            with conn.cursor() as cur:
                today = datetime.now().date()

                # 1. 赛道 TVL 数量
                cur.execute(
                    """
                    SELECT COUNT(*) FROM biz.category_tvl_daily
                    WHERE snapshot_date = %s
                    """,
                    (today,),
                )
                cat_cnt = cur.fetchone()[0]
                if cat_cnt < 50:
                    problems.append(f"赛道TVL仅 {cat_cnt} 个（<50阈值）")

                # 2. 大盘快照是否存在
                cur.execute(
                    """
                    SELECT COUNT(*) FROM biz.market_snapshot_daily
                    WHERE snapshot_date = %s
                    """,
                    (today,),
                )
                snap_cnt = cur.fetchone()[0]
                if snap_cnt == 0:
                    problems.append("今日大盘快照未写入")

                # 3. BTC OI 今日数据
                cur.execute(
                    """
                    SELECT COUNT(*) FROM biz.btc_oi_daily
                    WHERE metric_date = %s
                    """,
                    (today,),
                )
                row = cur.fetchone()
                oi_cnt = row[0] if row else 0
                if oi_cnt == 0:
                    problems.append("今日BTC OI数据为空")

        if not problems:
            return True, f"赛道 {cat_cnt} 个 · 快照 {snap_cnt} 条 · BTC OI {oi_cnt} 条 · 数据完整"
        return False, "；".join(problems)

    except Exception as e:
        return False, f"校验异常: {e}"


# 完整性校验函数映射
_INTEGRITY_CHECKS = {
    "check_market_daily_integrity": check_market_daily_integrity,
}


# ══════════════════════════════════════════════════════════════
# 核心检查逻辑
# ══════════════════════════════════════════════════════════════

def _check_staleness(key: str, desc: str, threshold: int, check_only: bool) -> dict:
    """检查停滞：超阈值未 done → 告警 + 补跑。返回结果 dict。"""
    last = _last_done_ts(key)
    now = time.time()

    if last is None:
        # 从未成功过 —— 只记录不告警（可能是新部署/新加入白名单）
        return {"key": key, "ok": True, "stale": False, "last_done": None,
                "reason": "从未成功（新任务或无历史）"}
    else:
        stale_for = now - last
        stale = stale_for > threshold
        reason = f"已 {stale_for/3600:.1f}h 未成功"

    if not stale:
        return {"key": key, "ok": True, "stale": False, "last_done": last}

    # 静默期抑制重复告警
    last_alert = _last_alerted.get(key, 0)
    if now - last_alert < ALERT_COOLDOWN_SECONDS:
        return {"key": key, "ok": False, "stale": True, "alerted": False,
                "reason": reason + "（静默期内）", "stale_for": stale_for}

    hours = round(stale_for / 3600, 1)

    # 告警邮件（check_only 模式不发）
    mail_ok = False
    if not check_only:
        _last_alerted[key] = now
        subject = f"⚠️ [看护] 调度任务 {key} 停滞 {hours}h"
        body = (
            f"关键 cron「{key}」({desc}) 已 {hours} 小时无成功执行。\n"
            f"最近一次成功: {datetime.fromtimestamp(last, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"预期频率: 每 {threshold//3600}h （阈值）\n\n"
            f"将自动补跑 {key}。"
        )
        mail_ok = _send_alert_email(subject, body)

    # 补跑（check_only 时不补）
    task_id = None
    if not check_only:
        import scheduler as _sched
        for _key, _cron, script, a, d, cat in _sched.SCHEDULE:
            if _key == key:
                task_id = submit_scheduled_task(key, script, a, d, category=cat)
                break

    return {
        "key": key, "ok": False, "stale": True, "alerted": mail_ok,
        "stale_for": stale_for, "mail_ok": mail_ok, "rerun_task_id": task_id,
        "reason": reason + ("，已告警" if mail_ok else "") + ("并补跑" if task_id else ""),
    }


def _check_failure(key: str, desc: str, check_only: bool) -> dict:
    """检查最近一次执行是否失败。失败则告警（单次失败不补跑，避免恶性循环）。"""
    last = _last_task_record(key)
    if not last:
        return {"key": key, "ok": True, "failed": False}

    status = last.get("status", "")
    if status in ("done", "running", "pending"):
        return {"key": key, "ok": True, "failed": False}

    # fail / error / killed 等异常状态
    now = time.time()

    # 静默期
    last_alert = _last_fail_alerted.get(key, 0)
    if now - last_alert < ALERT_COOLDOWN_SECONDS:
        return {"key": key, "ok": False, "failed": True, "alerted": False,
                "reason": "最近执行失败（静默期内）"}

    # check_only 模式不发邮件
    mail_ok = False
    if not check_only:
        _last_fail_alerted[key] = now

        err = last.get("error") or "未知错误"
        if isinstance(err, str) and len(err) > 200:
            err = err[:200] + "..."

        subject = f"❌ [看护] 调度任务 {key} 执行失败"
        body = (
            f"任务「{key}」({desc}) 最近一次执行失败。\n"
            f"状态: {status}\n"
            f"结束时间: {last['ended_at'].strftime('%Y-%m-%d %H:%M UTC') if last.get('ended_at') else '未知'}\n"
            f"错误: {err}\n\n"
            f"请登录工作台查看完整日志。"
        )
        mail_ok = _send_alert_email(subject, body)

    return {
        "key": key, "ok": False, "failed": True, "alerted": mail_ok,
        "mail_ok": mail_ok, "reason": f"执行失败: {status}",
    }


def _check_integrity(key: str, check_func_name: str | None, check_only: bool) -> dict:
    """数据完整性校验（仅对配置了校验函数的任务）。"""
    if not check_func_name:
        return {"key": key, "ok": True, "integrity": "skipped"}

    func = _INTEGRITY_CHECKS.get(check_func_name)
    if not func:
        return {"key": key, "ok": True, "integrity": "no_checker"}

    try:
        healthy, detail = func()
    except Exception as e:
        return {"key": key, "ok": False, "integrity": "error",
                "reason": f"校验异常: {e}"}

    if healthy:
        return {"key": key, "ok": True, "integrity": "ok", "detail": detail}

    # 不健康 → 告警（check_only 模式不发邮件）
    now = time.time()
    integ_key = f"integrity::{key}"
    last_alert = _last_alerted.get(integ_key, 0)
    if now - last_alert < ALERT_COOLDOWN_SECONDS:
        return {"key": key, "ok": False, "integrity": "fail",
                "alerted": False, "detail": detail, "reason": "数据不完整（静默期内）"}

    mail_ok = False
    if not check_only:
        _last_alerted[integ_key] = now

        subject = f"📊 [看护] {key} 数据完整性告警"
        body = (
            f"任务「{key}」数据完整性校验不通过：\n{detail}\n\n"
            f"请检查相关数据库表和日志。"
        )
        mail_ok = _send_alert_email(subject, body)

    return {
        "key": key, "ok": False, "integrity": "fail",
        "alerted": mail_ok, "mail_ok": mail_ok,
        "detail": detail, "reason": "数据不完整" + ("，已告警" if mail_ok else ""),
    }


def _send_daily_report() -> None:
    """每日健康总览邮件（09:00 左右发一次）。"""
    global _last_daily_report_date

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 只在 DAILY_REPORT_HOUR 这一小时内触发，且每天只发一次
    if now.hour != DAILY_REPORT_HOUR:
        return
    if _last_daily_report_date == today_str:
        return

    # 收集所有任务的状态
    lines = []
    healthy_count = 0
    problem_count = 0

    for key, desc, threshold, freq, integrity_fn in KEY_JOBS:
        last = _last_task_record(key)
        last_done = _last_done_ts(key)

        if not last:
            status_str = "⚠️ 无记录"
            problem_count += 1
        elif last.get("status") == "done":
            status_str = "✅ 正常"
            healthy_count += 1
        elif last.get("status") in ("running", "pending"):
            status_str = "⏳ 执行中"
            healthy_count += 1
        else:
            status_str = f"❌ 失败({last.get('status')})"
            problem_count += 1

        last_time = "—"
        if last_done:
            last_time = datetime.fromtimestamp(last_done, tz=timezone.utc).strftime("%m-%d %H:%M")
        elif last and last.get("ended_at"):
            last_time = last["ended_at"].strftime("%m-%d %H:%M")

        lines.append(f"  {status_str}  {key:25s}  {last_time:>14s}  {desc}")

    subject = f"📋 [看护日报] {today_str} 调度健康总览（{healthy_count}✅ {problem_count}❌）"
    body = (
        f"日期: {today_str}\n"
        f"健康: {healthy_count}  |  异常: {problem_count}  |  总计: {len(KEY_JOBS)}\n"
        f"{'=' * 60}\n\n"
    )
    body += "\n".join(lines)
    body += (
        f"\n\n{'=' * 60}\n"
        f"阈值: 每日任务 28h / 6h任务 9h / 12h任务 16h\n"
        f"轮询间隔: {POLL_SECONDS // 60} 分钟\n"
        f"告警静默: {ALERT_COOLDOWN_SECONDS // 3600} 小时\n"
    )

    mail_ok = _send_alert_email(subject, body)
    if mail_ok:
        _last_daily_report_date = today_str
        print(f"[看护] 每日健康日报已发送")
    else:
        print(f"[看护] 每日健康日报发送失败（可能未配置 SMTP）")


def run_once(check_only: bool = False) -> int:
    """执行一次完整检查：停滞检测 + 失败检测 + 完整性校验。"""
    print(f"\n{'='*60}")
    print(f"[看护] 开始巡检 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    results = []
    for key, desc, threshold, freq, integrity_fn in KEY_JOBS:
        # 1. 停滞检测
        r_stale = _check_staleness(key, desc, threshold, check_only)
        # 2. 失败检测（只有没在跑且没超期时才查，避免重复告警）
        r_fail = _check_failure(key, desc, check_only)
        # 3. 数据完整性
        r_integ = _check_integrity(key, integrity_fn, check_only)

        # 汇总
        ok = r_stale["ok"] and r_fail["ok"] and r_integ["ok"]
        status_str = "✅" if ok else "❌"
        detail_parts = []
        if not r_stale["ok"]:
            detail_parts.append(r_stale.get("reason", "停滞"))
        if not r_fail["ok"]:
            detail_parts.append(r_fail.get("reason", "失败"))
        if not r_integ["ok"]:
            detail_parts.append(r_integ.get("reason", "数据异常"))
        detail = " | ".join(detail_parts) if detail_parts else "正常"

        print(f"  {status_str} {key:25s}  {detail}")
        results.append({"key": key, "ok": ok, "desc": desc,
                        "stale": r_stale, "fail": r_fail, "integrity": r_integ})

    # 心跳
    _write_heartbeat()

    # 每日健康日报（仅在指定小时触发）
    try:
        _send_daily_report()
    except Exception as e:
        print(f"[看护] 日报发送异常: {e}", file=sys.stderr)

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n[看护] 巡检完成: {ok_count}/{len(results)} 正常")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="调度健康看护（增强版）")
    parser.add_argument("--once", action="store_true", help="单次检查后退出")
    parser.add_argument("--check-only", action="store_true", help="只检查，不发告警不补跑")
    parser.add_argument("--report", action="store_true", help="立即发送每日健康报告（测试用）")
    args = parser.parse_args()

    print(f"[看护] 启动，轮询每 {POLL_SECONDS}s（{POLL_SECONDS//60}min）")
    print(f"[看护] 告警静默: {ALERT_COOLDOWN_SECONDS//3600}h | 日报时间: {DAILY_REPORT_HOUR}:00")
    print(f"[看护] 监控任务数: {len(KEY_JOBS)}")
    print(f"[看护] 日志目录: {LOG_DIR}")

    if args.report:
        _last_daily_report_date = None  # 强制触发
        _send_daily_report()
        return 0

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
