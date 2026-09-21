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

去重：biz.scan_stall_alert（task='watchdog_scan'），停摆持续期间每 6 小时重发一封汇总邮件。
恢复：数据全部恢复新鲜时，若存在历史告警 → 发「已恢复」邮件并清空告警时间戳（下次停摆立即可告警）。

用法：
    python check_scan_freshness.py             # 检查 + 告警（cron 用）
    python check_scan_freshness.py --dry-run   # 只打印判断结果，不发送不写状态
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402

WATCHDOG_TASK_KEY = "watchdog_scan"
KLINE_MAX_AGE_MIN = 30
OI_MAX_AGE_MIN = 30
SIGNAL_MAX_AGE_MIN = 60
REALERT_INTERVAL_H = 6


def _fmt_utc(dt: datetime | None) -> str:
    if dt is None:
        return "无数据"
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")


def _collect_items(conn) -> list[dict]:
    """查三项数据的最新时间戳，返回检查项列表。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT MAX(open_time) AS mx FROM biz.asset_klines WHERE interval='15m'")
        mx_k = cur.fetchone()["mx"]
        cur.execute("SELECT MAX(ts) AS mx FROM biz.oi_cvd_snapshot WHERE exchange='binance'")
        mx_oi = cur.fetchone()["mx"]
        cur.execute("SELECT MAX(signal_ts) AS mx FROM biz.scan_signal")
        mx_sig = cur.fetchone()["mx"]

    now = datetime.now(timezone.utc)
    items = []
    for name, mx, threshold in (
        ("15m K线", mx_k, KLINE_MAX_AGE_MIN),
        ("OI 采样", mx_oi, OI_MAX_AGE_MIN),
        ("扫描信号", mx_sig, SIGNAL_MAX_AGE_MIN),
    ):
        if mx is None:
            items.append({"name": name, "mx": None, "age_min": None,
                          "threshold": threshold, "stale": True})
            continue
        age = (now - mx).total_seconds() / 60
        items.append({"name": name, "mx": mx, "age_min": age,
                      "threshold": threshold, "stale": age > threshold})
    return items


def _get_alert_state(conn) -> datetime | None:
    """读 watchdog 的上次告警时间（None=无记录或从未告警）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT last_email_ts FROM biz.scan_stall_alert WHERE task=%s",
            (WATCHDOG_TASK_KEY,),
        )
        row = cur.fetchone()
    return row["last_email_ts"] if row else None


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
        rows.append(
            f"<tr><td style='padding:4px 10px;border:1px solid #ddd'>{it['name']}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>{mark}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>{detail}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>阈值 {it['threshold']} 分钟</td></tr>"
        )
    return ("<table style='border-collapse:collapse;font-size:13px'>"
            "<tr><th style='padding:4px 10px;border:1px solid #ddd'>数据</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>状态</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>最新时间</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>—</th></tr>"
            + "".join(rows) + "</table>")


def main() -> int:
    parser = argparse.ArgumentParser(description="盘面扫描外部看门狗（数据新鲜度）")
    parser.add_argument("--dry-run", action="store_true", help="只打印判断，不发送邮件不写状态")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        items = _collect_items(conn)
        stale_items = [it for it in items if it["stale"]]
        last_alert = _get_alert_state(conn)
        now = datetime.now(timezone.utc)

        print(f"[看门狗] {now:%m-%d %H:%M} UTC 检查：")
        for it in items:
            age = f"{it['age_min']:.0f} 分钟" if it["age_min"] is not None else "无数据"
            mark = "STALE" if it["stale"] else "ok"
            print(f"  {it['name']:<8} {age:>10}  [{mark}] (阈值 {it['threshold']}m)")

        if not stale_items:
            # 全部新鲜 → 若有历史告警则发恢复邮件并清空状态
            if last_alert is None:
                print("[看门狗] 数据正常，无历史告警，结束")
                return 0
            gap_h = (now - last_alert).total_seconds() / 3600
            print(f"[看门狗] 数据已恢复（上次告警 {gap_h:.1f}h 前）→ 发恢复邮件")
            if args.dry_run:
                print("[看门狗] dry-run：跳过恢复邮件发送")
                return 0
            ok, msg = _send_mail(
                settings,
                "✅ 盘面扫描数据已恢复",
                "<h2 style='margin:0'>✅ 盘面扫描数据已恢复</h2>"
                f"<p>停摆告警（{_fmt_utc(last_alert)} 发出）后，采集已恢复正常：</p>"
                + _render_items_html(items)
                + "<p style='color:#999;font-size:12px'>盘面信号外部看门狗 · 自动邮件</p>",
            )
            if ok:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE biz.scan_stall_alert SET last_email_ts=NULL, updated_at=NOW() "
                        "WHERE task=%s", (WATCHDOG_TASK_KEY,))
                conn.commit()
                print("[看门狗] 恢复邮件已发送，告警状态已清空")
            else:
                print(f"[看门狗] 恢复邮件发送失败: {msg}", file=sys.stderr)
            return 0

        # 有停摆项 → 6h 去重后发汇总告警
        if last_alert is not None:
            gap_h = (now - last_alert).total_seconds() / 3600
            if gap_h < REALERT_INTERVAL_H:
                print(f"[看门狗] 停摆持续中，距上次告警仅 {gap_h:.1f}h（<{REALERT_INTERVAL_H}h），去重跳过")
                return 1

        names = "、".join(it["name"] for it in stale_items)
        ages = ", ".join(
            f"{it['name']}停在{_fmt_utc(it['mx'])}" if it["mx"] is not None
            else f"{it['name']}表为空" for it in stale_items)
        print(f"[看门狗] 停摆项: {names}" + ("（dry-run 不发送）" if args.dry_run else " → 发告警邮件"))
        if args.dry_run:
            return 1

        ok, msg = _send_mail(
            settings,
            f"🔴 盘面扫描数据停摆：{names} 已停更",
            "<h2 style='margin:0'>🔴 盘面扫描数据停摆告警（外部看门狗）</h2>"
            f"<p>以下数据超过阈值未更新，主池/蓄势池扫描已无法产出有效信号：</p>"
            + _render_items_html(items)
            + "<p><b>可能原因</b>：scan_daemon 进程崩溃/未启动、Binance IP 限频、"
              "Zeabur 部署被移除或容器未运行。</p>"
            "<p style='color:#999;font-size:12px'>"
            "本邮件由 scheduler 独立调度（每小时），不依赖 scan_daemon 存活；"
            f"停摆期间每 {REALERT_INTERVAL_H} 小时重发一次，恢复后自动发送解除通知。</p>",
        )
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at) "
                    "VALUES (%s, NOW(), NOW()) "
                    "ON CONFLICT (task) DO UPDATE SET "
                    "last_email_ts=EXCLUDED.last_email_ts, updated_at=EXCLUDED.updated_at",
                    (WATCHDOG_TASK_KEY,),
                )
            conn.commit()
            print("[看门狗] 停摆告警邮件已发送")
            return 1
        print(f"[看门狗] 停摆告警邮件发送失败: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
