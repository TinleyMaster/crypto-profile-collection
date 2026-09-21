#!/usr/bin/env python3
"""盘面扫描外部看门狗（check_scan_freshness）——独立单次检查，由 scheduler 每小时调度。

与 scan_daemon 内置停摆告警（_check_and_alert_stall）的区别：
  - daemon 内置告警挂在 daemon 进程内，daemon 崩溃/未启动时随之失效（2026-09-18 盲点：
    Zeabur 部署被移除，采集静默停摆 63 小时无人知晓）
  - 本脚本由 scheduler 独立调度，只看「数据是否停更」这一最终事实：
    scan_daemon 进程死亡、线程卡死、Binance 限频、部署被移除等任何原因导致的数据
    停摆都会被发现（只要 scheduler 所在容器还活着）

检查项与阈值：
  - 15m K线   MAX(open_time)  > 30 分钟（与 daemon 停摆告警同口径）
  - OI 采样   MAX(ts)         > 30 分钟（exchange='binance'）
  - 扫描信号  MAX(signal_ts)  > 60 分钟（主池 15min / 蓄势池 30min，护栏跳过陈旧币时放宽）

去重：biz.scan_stall_alert（task='scan_stall'）——与 scan_daemon 内置停摆告警
（_check_and_alert_stall）共用同一去重键与 6h 静默期，同一停摆事件只发一封邮件。
停摆持续期间每 6 小时重发一封汇总邮件。
恢复：数据全部恢复新鲜时，若存在历史告警 → 发「已恢复」邮件并清空告警时间戳（下次停摆立即可告警）。

退出码：0 = 检查本身执行成功（无论是否发现停摆；停摆用邮件+日志表达），
非 0 仅代表脚本自身异常（DB 连不上等）。scheduler 把非 0 视为「任务失败」并发
管理员失败邮件，故停摆判断不能借退出码表达，否则每次检查都会误报任务失败。

用法：
    python check_scan_freshness.py             # 检查 + 告警（cron 用）
    python check_scan_freshness.py --dry-run   # 只打印判断结果，不发送不写状态
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402

WATCHDOG_TASK_KEY = "scan_stall"
# 健康提示独立去重键（SQZ-01/06）：不复用 scan_stall，避免「健康提示」与真正的
# 「数据停摆」互相抑制 6h（两者成因与解除条件都不同）。
HEALTH_TASK_KEY = "squeeze_health"
KLINE_MAX_AGE_MIN = 30
OI_MAX_AGE_MIN = 30
SIGNAL_MAX_AGE_MIN = 60
REALERT_INTERVAL_H = 6
# 任务心跳：3× 任务周期（scan_klines/oi/liquidation/alert/squeeze 300s→15min，
# main_pool 900s→45min，accumulation/watchlist 1800s→90min，prune 24h→72h），
# 与 scan_daemon.STALL_HEARTBEAT_GRACE 同口径，且必须与 TASK_DEFS 全覆盖
# （审计 P0-B：原先漏了 squeeze/liquidation/watchlist，而 2026-09-21 停摆里
# 唯一留下物证的任务恰恰是盲区中的 scan_squeeze）。
# 判据读 last_ok_at（最近一次成功）而非 last_run_at —— 见 _collect_items 注释。
HEARTBEAT_MAX_AGE_MIN = {
    "scan_klines": 15, "scan_oi_cvd": 15, "scan_liquidation": 15,
    "scan_alert": 15, "scan_squeeze": 15,
    "scan_main_pool": 45, "scan_accumulation": 90, "watchlist_monitor": 90,
    "expire_signals": 90, "prune_scan_data": 4320,
}
# scan_daemon 启动时写的进程标记（last_run_at = 进程启动时刻）。据此区分
# 「本实例刚重启、某线程首轮还没跑完」（宽限，不报）与「线程从未启动」（真故障）。
DAEMON_START_TASK = "__daemon__"
# 交叉判据映射（审计 P2-7）：数据项 → 产出该数据的采集任务。
# 「数据停摆 + 对应任务心跳正常」= 进程健康而数据不落库 ⇒ 疑似静默失败
# （2026-09-21 实况：stdout 断开后每轮 print 抛 ValueError、func() 从未执行，
# 而 last_run_at 照常推进）。
DATA_SOURCE_TASKS = {
    "15m K线": ("scan_klines",),
    "OI 实时采样": ("scan_oi_cvd",),
    "扫描信号": ("scan_alert", "scan_main_pool"),
}


def _fmt_utc(dt: datetime | None) -> str:
    if dt is None:
        return "无数据"
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")


def _detect_silent_failure(items: list[dict]) -> list[str]:
    """交叉判据：数据停摆，但其采集任务按 last_ok_at 判为正常 → 疑似静默失败。"""
    hits: list[str] = []
    for data_name, tasks in DATA_SOURCE_TASKS.items():
        data_item = next((it for it in items if it["name"] == data_name), None)
        if data_item is None or not data_item["stale"]:
            continue
        for task in tasks:
            task_item = next(
                (it for it in items if it.get("task") == task), None)
            if task_item is None or task_item["stale"]:
                continue
            hits.append(
                f"{data_name}已停更（{_fmt_utc(data_item['mx'])}），但任务 {task} "
                f"最近一次成功在 {_fmt_utc(task_item['mx'])}（判为正常）")
            break
    return hits


def _collect_items(conn) -> list[dict]:
    """查数据最新时间戳与任务心跳，返回检查项列表。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT MAX(open_time) AS mx FROM biz.asset_klines WHERE interval='15m'")
        mx_k = cur.fetchone()["mx"]
        # OI 只看实时 5m 采样：历史回填写入的 1h 行会让 MAX(ts) 假新鲜（审计 P0-2）
        cur.execute(
            "SELECT MAX(ts) AS mx FROM biz.oi_cvd_snapshot "
            "WHERE exchange='binance' AND source='realtime'")
        mx_oi = cur.fetchone()["mx"]
        cur.execute("SELECT MAX(signal_ts) AS mx FROM biz.scan_signal")
        mx_sig = cur.fetchone()["mx"]

    now = datetime.now(timezone.utc)
    items = []
    for name, mx, threshold in (
        ("15m K线", mx_k, KLINE_MAX_AGE_MIN),
        ("OI 实时采样", mx_oi, OI_MAX_AGE_MIN),
        ("扫描信号", mx_sig, SIGNAL_MAX_AGE_MIN),
    ):
        if mx is None:
            items.append({"name": name, "mx": None, "age_min": None,
                          "threshold": threshold, "stale": True})
            continue
        age = (now - mx).total_seconds() / 60
        items.append({"name": name, "mx": mx, "age_min": age,
                      "threshold": threshold, "stale": age > threshold})

    # 任务心跳（表不存在时跳过，兼容迁移未执行的部署）
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT task, last_run_at, last_ok_at, last_error FROM biz.scan_heartbeat")
            hb = {r["task"]: r for r in cur.fetchall()}
        start_row = hb.get(DAEMON_START_TASK)
        daemon_start = start_row["last_run_at"] if start_row else None
        for task, threshold in HEARTBEAT_MAX_AGE_MIN.items():
            row = hb.get(task)
            last_run = row["last_run_at"] if row else None
            # 本实例尚未跑完首轮（无心跳，或心跳来自上一次进程）→ 以进程启动时刻
            # 起算宽限：超过阈值才判停摆，避免每次部署后立刻误报。
            if row is None or (daemon_start is not None and last_run < daemon_start):
                if daemon_start is None:
                    items.append({"name": f"任务{task}", "task": task, "mx": None,
                                  "age_min": None,
                                  "threshold": threshold, "stale": True,
                                  "note": "该线程可能从未启动"})
                    continue
                age = (now - daemon_start).total_seconds() / 60
                items.append({"name": f"任务{task}首轮", "task": task, "mx": daemon_start,
                              "age_min": age,
                              "threshold": threshold, "stale": age > threshold,
                              "note": "本实例尚未跑完首轮"})
                continue
            # 判据用 last_ok_at（最近一次**成功**）而非 last_run_at：stdout/日志
            # 设施故障时每轮都在 print 处抛 ValueError，func() 从未执行，而
            # last_run_at 照常推进 —— 只看 last_run_at 会把「数据停摆 50 分钟、
            # 任务行却全绿」判成正常（审计 P0-B，2026-09-21 实物证据）。
            # 未成功过（last_ok_at IS NULL）→ 以进程启动时刻起算宽限。
            base = row["last_ok_at"] or daemon_start
            if base is None:
                items.append({"name": f"任务{task}", "task": task, "mx": None,
                              "age_min": None,
                              "threshold": threshold, "stale": True,
                              "note": "从未成功执行"})
                continue
            age = (now - base).total_seconds() / 60
            err = row["last_error"]
            items.append({"name": f"任务{task}", "task": task, "mx": base, "age_min": age,
                          "threshold": threshold, "stale": age > threshold,
                          "note": (f"线程在跑但连续失败：{err}" if err else
                                   ("最近一次成功" if age > threshold else ""))})
    except Exception as e:  # noqa: BLE001
        print(f"[看门狗] 心跳检查跳过（{e}）", file=sys.stderr)
    return items


def _get_alert_state(conn, key: str = WATCHDOG_TASK_KEY) -> datetime | None:
    """读指定去重键的上次告警时间（None=无记录或从未告警）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT last_email_ts FROM biz.scan_stall_alert WHERE task=%s",
            (key,),
        )
        row = cur.fetchone()
    return row["last_email_ts"] if row else None


def _get_all_alert_states(conn) -> dict[str, datetime]:
    """读所有告警去重键的非空时间（恢复时需清空全部）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT task, last_email_ts FROM biz.scan_stall_alert "
            "WHERE task = ANY(%s) AND last_email_ts IS NOT NULL",
            ([WATCHDOG_TASK_KEY, HEALTH_TASK_KEY],))
        return {r["task"]: r["last_email_ts"] for r in cur.fetchall()}


def _send_mail(settings, subject: str, body: str) -> tuple[bool, str]:
    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        return False, "SMTP 未配置（缺少 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO）"
    return notifier.send(subject, body, from_name="盘面信号看门狗")


def _render_items_html(items: list[dict]) -> str:
    rows = []
    for it in items:
        if it["mx"] is None:
            detail = "表为空"
        else:
            detail = f"最新 {_fmt_utc(it['mx'])}（距今 {it['age_min']:.0f} 分钟）"
        mark = "🔴 停摆" if it["stale"] else "🟢 正常"
        # note（心跳语义：线程在跑但连续失败 / 从未启动 / 首轮宽限）必须渲染出来，
        # 否则「数据停摆、任务全绿」的自相矛盾会原样复现（审计 P0-B / P2-5）。
        note = it.get("note") or ""
        rows.append(
            f"<tr><td style='padding:4px 10px;border:1px solid #ddd'>{it['name']}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>{mark}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>{detail}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>阈值 {it['threshold']} 分钟</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd;color:#888'>{note}</td></tr>"
        )
    return ("<table style='border-collapse:collapse;font-size:13px'>"
            "<tr><th style='padding:4px 10px;border:1px solid #ddd'>数据</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>状态</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>最新时间</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>阈值</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>说明</th></tr>"
            + "".join(rows) + "</table>")


# 轧空池/采样健康（工单 SQZ-01/06）：只读观测，越线才渲染，避免告警噪音。
SQUEEZE_TRACKING_WARN = 9        # 队列占用 ≥ 9/12（75%）
SQUEEZE_ENQ24H_WARN = 20         # 近 24h 入队 ≥ 20 条
SQUEEZE_REJECT_WARN = 3          # 近 7 天覆盖率/尾部拒判 ≥ 3 次
RESTART_GAP_WINDOW_H = 2         # 重启丢桶观察窗（小时）
OI_BUCKET_DEFICIT_RATIO = 0.9    # 近 2h OI 桶数 < 期望 ×0.9 → 判为缺口
SQUEEZE_QUEUE_MAX = 12           # 与 squeeze.TRACK_QUEUE_MAX 同口径（展示用）


def _collect_squeeze_health(conn) -> list[str]:
    """轧空池健康 + 重启丢桶（SQZ-01/06）：返回越线提示；未越线返回 []。

    - 队列占用 / 24h 入队 / 覆盖率拒判：只读 `biz.squeeze_track`（reason 由
      `scan_daemon` 拒判分支显式写入，无需改表）。
    - 重启丢桶：`__daemon__` 近 `RESTART_GAP_WINDOW_H` 小时内启动过，但该窗口
      `oi_cvd_snapshot` 的 5m 桶数不足期望 → 快照不可回补，会抬高覆盖率拒判率
      （工单 SQZ-06，与本文件既有 OI 新鲜度检查互补：后者只看 MAX(ts)，看不到
      中段/尾部缺桶）。
    """
    notes: list[str] = []
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE status='tracking') AS tracking, "
                "       count(*) FILTER (WHERE status='judged')   AS judged, "
                "       count(*) FILTER (WHERE status='expired')  AS expired, "
                "       count(*) FILTER (WHERE started_at > NOW() - INTERVAL '24 hours') "
                "                                               AS enq_24h, "
                "       count(*) FILTER (WHERE status='tracking' "
                "                          AND reason LIKE '判定窗口%') AS reject "
                "FROM biz.squeeze_track")
            row = cur.fetchone()
        if row["tracking"] >= SQUEEZE_TRACKING_WARN:
            notes.append(f"轧空队列占用 {row['tracking']}/{SQUEEZE_QUEUE_MAX}"
                         f"（≥{SQUEEZE_TRACKING_WARN}，接近上限）")
        if row["enq_24h"] >= SQUEEZE_ENQ24H_WARN:
            notes.append(f"轧空池近 24h 入队 {row['enq_24h']} 条（≥{SQUEEZE_ENQ24H_WARN}）")
        if row["reject"] >= SQUEEZE_REJECT_WARN:
            notes.append(f"覆盖率/尾部闸门拒判 {row['reject']} 次（≥{SQUEEZE_REJECT_WARN}）")
    except Exception as e:  # noqa: BLE001
        print(f"[看门狗] 轧空池健康检查跳过（{e}）", file=sys.stderr)

    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT last_run_at FROM biz.scan_heartbeat WHERE task=%s",
                        (DAEMON_START_TASK,))
            r = cur.fetchone()
            daemon_start = r["last_run_at"] if r else None
            now = datetime.now(timezone.utc)
            if daemon_start is not None \
                    and (now - daemon_start).total_seconds() <= RESTART_GAP_WINDOW_H * 3600:
                cur.execute(
                    "SELECT count(DISTINCT ts) AS n FROM biz.oi_cvd_snapshot "
                    "WHERE exchange='binance' AND source='realtime' "
                    "AND ts >= %s AND ts <= %s",
                    (now - timedelta(hours=RESTART_GAP_WINDOW_H), now))
                have = cur.fetchone()["n"] or 0
                expect = int(RESTART_GAP_WINDOW_H * 60 / 5)   # 5m 桶数
                if have < expect * OI_BUCKET_DEFICIT_RATIO:
                    notes.append(
                        f"daemon 近 {RESTART_GAP_WINDOW_H}h 内重启过"
                        f"（{_fmt_utc(daemon_start)}），该窗口 OI 桶仅 {have}/{expect}"
                        f"（快照不可回补，会抬高覆盖率拒判）")
    except Exception as e:  # noqa: BLE001
        print(f"[看门狗] 重启丢桶检查跳过（{e}）", file=sys.stderr)
    return notes


def _render_health_html(notes: list[str]) -> str:
    return ("<h3 style='margin:18px 0 6px'>🩺 轧空池 / 采样健康（只读观测）</h3>"
            "<ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>")


def main() -> int:
    parser = argparse.ArgumentParser(description="盘面扫描外部看门狗（数据新鲜度）")
    parser.add_argument("--dry-run", action="store_true", help="只打印判断，不发送邮件不写状态")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        items = _collect_items(conn)
        stale_items = [it for it in items if it["stale"]]
        health_notes = _collect_squeeze_health(conn)
        now = datetime.now(timezone.utc)

        print(f"[看门狗] {now:%m-%d %H:%M} UTC 检查：")
        for it in items:
            age = f"{it['age_min']:.0f} 分钟" if it["age_min"] is not None else "无数据"
            mark = "STALE" if it["stale"] else "ok"
            print(f"  {it['name']:<8} {age:>10}  [{mark}] (阈值 {it['threshold']}m)")
        if health_notes:
            print("[看门狗] 轧空池/采样健康提示：")
            for n in health_notes:
                print(f"  - {n}")

        has_issue = bool(stale_items) or bool(health_notes)
        if not has_issue:
            # 全部正常 → 若任一去重键有历史告警则发恢复邮件并清空
            alerts = _get_all_alert_states(conn)
            if not alerts:
                print("[看门狗] 数据正常，无历史告警，结束")
                return 0
            last_any = min(alerts.values())
            gap_h = (now - last_any).total_seconds() / 3600
            print(f"[看门狗] 已恢复正常（上次告警 {gap_h:.1f}h 前）→ 发恢复邮件")
            if args.dry_run:
                print("[看门狗] dry-run：跳过恢复邮件发送")
                return 0
            ok, msg = _send_mail(
                settings,
                "✅ 盘面扫描数据已恢复",
                "<h2 style='margin:0'>✅ 盘面扫描数据已恢复</h2>"
                f"<p>告警（{_fmt_utc(last_any)} 发出）后已恢复正常：</p>"
                + _render_items_html(items)
                + "<p style='color:#999;font-size:12px'>盘面信号外部看门狗 · 自动邮件</p>",
            )
            if ok:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE biz.scan_stall_alert SET last_email_ts=NULL, updated_at=NOW() "
                        "WHERE task = ANY(%s)",
                        ([WATCHDOG_TASK_KEY, HEALTH_TASK_KEY],))
                conn.commit()
                print("[看门狗] 恢复邮件已发送，告警状态已清空")
            else:
                print(f"[看门狗] 恢复邮件发送失败: {msg}", file=sys.stderr)
            return 0

        # 有停摆项走 scan_stall 键；仅健康越线走 squeeze_health 键（互不抑制）
        alert_key = WATCHDOG_TASK_KEY if stale_items else HEALTH_TASK_KEY
        last_alert = _get_alert_state(conn, alert_key)
        if last_alert is not None:
            gap_h = (now - last_alert).total_seconds() / 3600
            if gap_h < REALERT_INTERVAL_H:
                print(f"[看门狗] 告警持续中，距上次邮件仅 {gap_h:.1f}h"
                      f"（<{REALERT_INTERVAL_H}h），去重跳过")
                return 0

        health_html = _render_health_html(health_notes) if health_notes else ""
        silent = _detect_silent_failure(items) if stale_items else []
        if stale_items:
            names = "、".join(it["name"] for it in stale_items)
            print(f"[看门狗] 停摆项: {names}"
                  + ("（dry-run 不发送）" if args.dry_run else " → 发告警邮件"))
            subject = f"🔴 盘面扫描数据停摆：{names} 已停更"
            title = "🔴 盘面扫描数据停摆告警（外部看门狗）"
            intro = "<p>以下数据超过阈值未更新，主池/蓄势池扫描已无法产出有效信号：</p>"
        else:
            print(f"[看门狗] 健康提示 {len(health_notes)} 项"
                  + ("（dry-run 不发送）" if args.dry_run else " → 发提示邮件"))
            subject = "⚠️ 盘面扫描健康提示（轧空池/采样缺口）"
            title = "⚠️ 盘面扫描健康提示（外部看门狗）"
            intro = ("<p>数据本身仍新鲜，但轧空池/采样出现以下情况："
                     "重启丢桶不可回补，会让覆盖率闸门更频繁拒判，需观察。</p>")
        if silent:
            print("[看门狗] 疑似静默失败（数据停摆但采集任务心跳判为正常）：")
            for s in silent:
                print(f"  - {s}")
        if args.dry_run:
            return 0

        # 交叉判据分节（审计 P2-7）：数据停摆 + 对应任务心跳正常 ⇒ 进程健康但
        # 数据不落库，属静默失败，成因与「进程没起来」完全不同，必须单独说清。
        silent_html = ""
        if silent:
            silent_html = (
                "<h3 style='margin:18px 0 6px'>🔍 疑似静默失败（进程健康、数据不落库）</h3>"
                "<p>下列数据的采集任务按 <code>last_ok_at</code> 判定为<b>正常</b>，"
                "但数据本身已停更 —— 说明守护进程在跑、任务函数却没有真正执行：</p>"
                "<ul>" + "".join(f"<li>{s}</li>" for s in silent) + "</ul>"
                "<p>最常见成因：容器日志设施断开后 <code>print()</code> 抛 "
                "<code>ValueError: I/O operation on closed file.</code>，异常被吞成"
                "「单轮失败」，业务函数从未执行，而心跳照常推进。"
                "新版 scan_daemon 已加固日志流（写失败即丢弃），且仅在「连续 3 轮连"
                "心跳都写不进 DB」（进程级故障）时才主动退出交 supervisord 重启；"
                "外部依赖抖动只记 last_error、不重启进程。若本邮件仍出现该分节，"
                "请在容器内执行 <code>supervisorctl restart scan_daemon</code>。</p>")
        cause_html = ""
        if stale_items:
            cause_html = (
                "<p><b>两类成因怎么区分</b>：<br>"
                "① 任务行说明列写「线程在跑但连续失败：…」⇒ <b>线程在跑、每轮都失败</b>"
                "（静默失败，见上节）；<br>"
                "② 任务项写「无心跳 / 从未启动」或整个表都没有任务行 ⇒ "
                "<b>进程或线程确实没起来</b>（进程崩溃、Zeabur 部署被移除、容器未运行）。<br>"
                "两类也都可能叠加 Binance IP 限频（418）。另注意："
                "<b>文件更新 ≠ 进程重启</b>，改了代码不重启容器不生效。</p>")

        ok, msg = _send_mail(
            settings,
            subject,
            f"<h2 style='margin:0'>{title}</h2>"
            + intro
            + _render_items_html(items)
            + health_html
            + silent_html
            + cause_html
            + "<p><b>其他可能原因</b>：scan_daemon 进程崩溃/未启动、Binance IP 限频、"
              "Zeabur 部署被移除或容器未运行。</p>"
            "<p style='color:#999;font-size:12px'>"
            "本邮件由 scheduler 独立调度（每小时），不依赖 scan_daemon 存活；"
            f"告警期间每 {REALERT_INTERVAL_H} 小时重发一次，恢复后自动发送解除通知。</p>",
        )
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at) "
                    "VALUES (%s, NOW(), NOW()) "
                    "ON CONFLICT (task) DO UPDATE SET "
                    "last_email_ts=EXCLUDED.last_email_ts, updated_at=EXCLUDED.updated_at",
                    (alert_key,),
                )
            conn.commit()
            print(f"[看门狗] 告警邮件已发送（去重键 {alert_key}）")
        else:
            print(f"[看门狗] 告警邮件发送失败: {msg}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
