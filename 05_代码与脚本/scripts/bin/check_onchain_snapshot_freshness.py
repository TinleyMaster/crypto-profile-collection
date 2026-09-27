#!/usr/bin/env python3
"""链上持仓快照外部看门狗（check_onchain_snapshot_freshness）——独立单次检查，由 scheduler 每小时调度。

背景（工单 OBI-OPT-SNAPSHOT-FRESHNESS X3，2026-09-27）：
  `biz.onchain_holder_snapshot` 原为分链**隔日**运行（`1,3,5` / `2,4,6`），每条链天然滞后
  1–2 天；且无外部看门狗——单轮 RPC 失败即整日空缺，隔日叠加后实测滞后可达 3 天而无人知晓。
  本脚本与 `phase_chain_holder_batch` 相互独立：只看「各链最新快照日」这一最终事实，
  调度未跑、脚本崩溃、RPC 大面积失败等任何原因造成的停更都会被发现。

检查项与阈值：
  - 每条链  MAX(snapshot_date)  距今 > 2 天（与「每日运行」设计对齐，留 1 天缓冲）

去重：复用 `biz.scan_stall_alert`（task='onchain_snapshot_stall'，与 scan 看门狗同表不同键，
  各自 6h 静默期互不抑制）——同一停摆事件 6h 内只发一封，持续期间每 6 小时重发一封汇总邮件；
  全部恢复新鲜后发「已恢复」邮件并清空告警时间戳（下次停摆立即可告警）。

退出码：0 = 检查本身执行成功（无论是否发现滞后；滞后用邮件+日志表达），
  非 0 仅代表脚本自身异常（DB 连不上等）。scheduler 把非 0 视为「任务失败」并发管理员失败邮件，
  故滞后判断不能借退出码表达，否则每次检查都会误报任务失败。

用法：
    python check_onchain_snapshot_freshness.py             # 检查 + 告警（cron 用）
    python check_onchain_snapshot_freshness.py --dry-run   # 只打印判断结果，不发送不写状态
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402

ALERT_TASK_KEY = "onchain_snapshot_stall"
MAX_STALE_DAYS = 2
REALERT_INTERVAL_H = 6
BJ = ZoneInfo("Asia/Shanghai")


def _collect_items(conn) -> list[dict]:
    """按链查最新快照日，返回 [{chain, mx, age_days, stale}]（按北京时间「今日」算龄，避免 UTC 偏差）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT chain, MAX(snapshot_date) AS mx "
            "FROM biz.onchain_holder_snapshot GROUP BY chain ORDER BY chain")
        rows = cur.fetchall()

    today = datetime.now(BJ).date()
    items: list[dict] = []
    for r in rows:
        mx = r["mx"]
        age_days = None if mx is None else (today - mx).days
        items.append({
            "chain": r["chain"], "mx": mx, "age_days": age_days,
            "stale": age_days is None or age_days > MAX_STALE_DAYS,
        })
    if not items:
        # 表整表为空 = 采集从未成功（比「滞后」更严重），按滞后告警而非静默通过。
        items.append({"chain": "（全部链无记录）", "mx": None, "age_days": None, "stale": True})
    return items


def _get_alert_state(conn) -> datetime | None:
    """读去重键的上次告警时间（None=无记录或从未告警）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            "SELECT last_email_ts FROM biz.scan_stall_alert WHERE task=%s",
            (ALERT_TASK_KEY,))
        row = cur.fetchone()
    return row["last_email_ts"] if row else None


def _render_html(items: list[dict]) -> str:
    rows = []
    for it in items:
        detail = ("无记录" if it["mx"] is None
                  else f"最新 {it['mx']}（距今 {it['age_days']} 天）")
        mark = "🔴 滞后" if it["stale"] else "🟢 正常"
        rows.append(
            f"<tr><td style='padding:4px 10px;border:1px solid #ddd'>{it['chain']}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>{mark}</td>"
            f"<td style='padding:4px 10px;border:1px solid #ddd'>{detail}</td></tr>"
        )
    return ("<table style='border-collapse:collapse;font-size:13px'>"
            "<tr><th style='padding:4px 10px;border:1px solid #ddd'>链</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>状态</th>"
            "<th style='padding:4px 10px;border:1px solid #ddd'>最新快照日</th></tr>"
            + "".join(rows) + "</table>")


def _send_mail(settings, subject: str, body: str) -> tuple[bool, str]:
    """发看门狗邮件——只发系统管理员（ADMIN_EMAIL），未配置时回退 SMTP_TO。"""
    from crypto_research.clients.notifier import EmailNotifier
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        return False, "SMTP 未配置（缺少 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO）"
    return notifier.send(subject, body, from_name="链上快照看门狗",
                         to=settings.admin_email or settings.smtp_to)


def main() -> int:
    parser = argparse.ArgumentParser(description="链上持仓快照外部看门狗（新鲜度）")
    parser.add_argument("--dry-run", action="store_true", help="只打印判断，不发送邮件不写状态")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
        items = _collect_items(conn)
        stale = [it for it in items if it["stale"]]
        now = datetime.now(timezone.utc)

        print(f"[看门狗] {now:%m-%d %H:%M} UTC 链上快照新鲜度检查：")
        for it in items:
            age = f"{it['age_days']} 天" if it["age_days"] is not None else "无记录"
            print(f"  {it['chain']:<12} {age:>8}  "
                  f"[{'STALE' if it['stale'] else 'ok'}] (阈值 {MAX_STALE_DAYS} 天)")

        last_alert = _get_alert_state(conn)

        if not stale:
            if last_alert is None:
                print("[看门狗] 全部链新鲜，无历史告警，结束")
                return 0
            gap_h = (now - last_alert).total_seconds() / 3600
            print(f"[看门狗] 已恢复正常（上次告警 {gap_h:.1f}h 前）→ 发恢复邮件")
            if args.dry_run:
                print("[看门狗] dry-run：跳过恢复邮件发送")
                return 0
            ok, msg = _send_mail(
                settings, "✅ 链上持仓快照已恢复",
                "<h2 style='margin:0'>✅ 链上持仓快照已恢复</h2>"
                f"<p>告警发出后各链已恢复每日快照：</p>{_render_html(items)}"
                "<p style='color:#999;font-size:12px'>链上快照外部看门狗 · 自动邮件</p>")
            if ok:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE biz.scan_stall_alert SET last_email_ts=NULL, updated_at=NOW() "
                        "WHERE task=%s", (ALERT_TASK_KEY,))
                conn.commit()
                print("[看门狗] 恢复邮件已发送，告警状态已清空")
            else:
                print(f"[看门狗] 恢复邮件发送失败: {msg}", file=sys.stderr)
            return 0

        if last_alert is not None:
            gap_h = (now - last_alert).total_seconds() / 3600
            if gap_h < REALERT_INTERVAL_H:
                print(f"[看门狗] 告警持续中，距上次邮件仅 {gap_h:.1f}h"
                      f"（<{REALERT_INTERVAL_H}h），去重跳过")
                return 0

        names = "、".join(it["chain"] for it in stale)
        print(f"[看门狗] 滞后链: {names}"
              + ("（dry-run 不发送）" if args.dry_run else " → 发告警邮件"))
        if args.dry_run:
            return 0

        ok, msg = _send_mail(
            settings, f"🔴 链上持仓快照滞后：{names}",
            "<h2 style='margin:0'>🔴 链上持仓快照滞后告警</h2>"
            f"<p>以下链的最新快照已超过 {MAX_STALE_DAYS} 天未更新：</p>{_render_html(items)}"
            "<p><b>排查方向</b>：① 调度是否在跑（容器内 "
            "<code>python scheduler.py --list | grep chain_holder</code>）；"
            "② 采集脚本是否因 RPC 失败/超时整轮空跑（查该任务日志）；"
            "③ chain 并发槽位是否被长任务占满。</p>"
            "<p style='color:#999;font-size:12px'>本邮件由 scheduler 独立调度（每小时）；"
            f"告警期间每 {REALERT_INTERVAL_H} 小时重发一次，恢复后自动发送解除通知。</p>")
        if ok:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO biz.scan_stall_alert (task, last_email_ts, updated_at) "
                    "VALUES (%s, NOW(), NOW()) "
                    "ON CONFLICT (task) DO UPDATE SET "
                    "last_email_ts=EXCLUDED.last_email_ts, updated_at=EXCLUDED.updated_at",
                    (ALERT_TASK_KEY,))
            conn.commit()
            print("[看门狗] 告警邮件已发送")
        else:
            print(f"[看门狗] 告警邮件发送失败: {msg}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())