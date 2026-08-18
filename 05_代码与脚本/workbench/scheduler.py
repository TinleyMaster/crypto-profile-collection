"""
定时调度器：用 APScheduler 按 cron 表达式触发采集脚本，替代 n8n 的调度职责。

背景：n8n 原本的「编排 / 调度 / 重试」职责已被代码吸收——
  - 依赖顺序编排 → run_*_pipeline.py 一键流水线
  - 读库取任务 + 循环   → *__auto.py 自循环（entry_count==0 自动终止）
  - 人工触发 + 日志     → Web 工作台 TaskManager
  - 定时监控 + 邮件     → phase_watchlist_monitor.py
所以 n8n 只剩「到点把脚本踢起来」，改由本调度器承担。

职责：
  1. 按调度表定时启动 scripts/bin 下的脚本（调用方式与工作台 TaskManager 一致）
  2. 同一任务不重叠（max_instances=1 + coalesce，前一轮未结束则跳过新触发）
  3. 子进程日志实时流式写入日志文件（也打到 stdout 便于 docker logs 查看）
  4. 子进程非零退出 → SMTP 邮件告警（复用 SMTP_HOST/PORT/USER/PASS/TO/FROM）

用法：
    python scheduler.py                 # 前台常驻
    python scheduler.py --list          # 打印调度表
    python scheduler.py --run-once <key>  # 立即同步执行某个任务一次（测试/补跑）

环境变量（均可选）：
    SCHEDULER_TIMEZONE  时区（默认 Asia/Shanghai）
    SCHEDULER_ENABLED   逗号分隔的 key 白名单，空 = 全部启用
    SCHEDULER_LOG_DIR   日志目录（默认 /app/task_state/scheduler 或本地 task_state/scheduler）
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_TO / SMTP_FROM  失败邮件告警
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

# Docker 或本地环境判断脚本路径（与 task_manager.py 保持一致）
if os.path.exists("/app/scripts/bin"):
    SCRIPTS_BIN = Path("/app/scripts/bin")
    _DEFAULT_STATE_DIR = Path("/app/task_state")
else:
    SCRIPTS_BIN = Path(__file__).resolve().parents[1] / "scripts" / "bin"
    _DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "task_state"

STATE_DIR = Path(os.getenv("SCHEDULER_LOG_DIR") or _DEFAULT_STATE_DIR)
LOG_DIR = STATE_DIR / "scheduler"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")

# 调度表：(key, cron, script, args, 说明)
# cron 为 5 段式（分 时 日 月 周）。所有 *__auto.py 都是「跑到完即停」，周期性触发安全。
SCHEDULE: list[tuple[str, str, str, list[str], str]] = [
    # ═══ 数据源流水线 ═══
    ("cmc_pipeline", "0 3 * * *", "run_cmc_pipeline.py", [], "CMC 一键流水线（每日）"),
    ("dl_pipeline", "0 4 * * *", "run_dl_pipeline.py", [], "DefiLlama 一键流水线（每日）"),
    ("cg_pipeline", "0 5 * * 1", "run_cg_pipeline.py", [], "CoinGecko 一键流水线（每周一，月配额 10k）"),

    # ═══ 链上快照（每日单次）═══
    ("chain_holder_snapshot_auto", "30 5 * * *", "phase_chain_holder_snapshot_auto.py", [], "链上持仓快照（每日单次）"),

    # ═══ 文档入口补充（自动循环）═══
    ("cmc_refresh_docs_auto", "0 6 * * *", "refresh_doc_source_entries_from_cmc_auto.py", [], "CMC 补充文档入口"),
    ("cg_refresh_docs_auto", "0 6 * * *", "refresh_doc_source_entries_from_cg_auto.py", [], "CG 补充文档入口"),
    ("dl_refresh_docs_auto", "0 6 * * *", "refresh_doc_source_entries_from_dl_auto.py", [], "DL 补充文档入口"),
    ("dual_supplement_auto", "0 7 * * *", "supplement_doc_entries_dual_auto.py", [], "双源(DexScreener+Binance)补充文档入口"),

    # ═══ 第三方专项 ═══
    ("third_party_auto", "0 8 * * *", "phase_b2_third_party_auto.py", [], "第三方评级/审计回填"),
    ("third_party_raises_auto", "0 8 * * *", "phase_b2_third_party_raises_auto.py", [], "TGE/融资轮次采集"),
    ("third_party_hacks", "30 8 * * 1", "phase_b2_third_party_hacks.py", [], "链上异常事件采集（每周一）"),

    # ═══ 文档深度爬取 ═══
    ("b2_auto_loop", "0 */6 * * *", "phase_b2_auto_loop.py", [], "B2 深度文档发现（每 6 小时）"),
    ("spa_browser_crawl_auto", "0 9 * * *", "phase_b2_spa_browser_crawl_auto.py", [], "B3 SPA 无头浏览器爬取"),
    ("b2_ai_noise_clean_by_asset_auto", "0 10 * * *", "phase_b2_ai_noise_clean_by_asset_auto.py", [], "B4 AI 噪声清理（按资产）"),

    # ═══ 监控告警 ═══
    ("chain_transfer_monitor_auto", "*/30 * * * *", "phase_chain_transfer_monitor_auto.py", [], "大额转账监控（跑到完）"),
    ("watchlist_monitor", "*/30 * * * *", "phase_watchlist_monitor.py", [], "解锁/空头/大户监控（单次）"),
]


def _log_path(key: str) -> Path:
    return LOG_DIR / f"{key}.log"


def _send_alert_email(subject: str, body_text: str) -> bool:
    """失败告警邮件（自包含 smtplib，不依赖 crypto_research）。"""
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    pwd = os.getenv("SMTP_PASS", "")
    to = os.getenv("SMTP_TO", "").strip()
    if not (host and user and pwd and to):
        print("[调度] SMTP 未配置，跳过失败告警邮件")
        return False

    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr

    port = int(os.getenv("SMTP_PORT", "465") or 465)
    from_addr = os.getenv("SMTP_FROM", "").strip() or user
    to_addrs = [a.strip() for a in to.split(",") if a.strip()]

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("调度器告警", "utf-8")), from_addr))
    msg["To"] = ", ".join(to_addrs)

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        server.login(user, pwd)
        server.sendmail(from_addr, to_addrs, msg.as_string())
        server.quit()
        print("[调度] 失败告警邮件已发送")
        return True
    except Exception as e:
        print(f"[调度] 告警邮件发送失败: {e}")
        return False


def _run_job(key: str, script: str, args: list[str]) -> int:
    """同步执行一个任务，实时写日志，非零退出发邮件。返回退出码。"""
    log_path = _log_path(key)
    script_path = str(SCRIPTS_BIN / script)
    cmd = [sys.executable, "-u", script_path] + list(args)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 60}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 启动 {key}: {' '.join(cmd)}\n")

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    tail: deque[str] = deque(maxlen=50)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPTS_BIN.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n").rstrip("\r")
            tail.append(line)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            print(f"[{key}] {line}")
        proc.wait()
        returncode = proc.returncode
    except Exception as e:
        returncode = -1
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[ERROR] 启动失败: {e}\n")
        print(f"[调度] {key} 启动失败: {e}")
        tail.append(f"[ERROR] 启动失败: {e}")

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 结束 {key} exit_code={returncode}\n")

    if returncode != 0:
        body = "\n".join(tail) or "(无日志)"
        _send_alert_email(
            f"⚠️ [调度] {key} 执行失败 (exit {returncode})",
            f"任务：{key}\n脚本：{script}\n命令：{' '.join(cmd)}\n退出码：{returncode}\n\n最近日志（最多 50 行）：\n{body}",
        )
    return returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="定时调度器（替代 n8n）")
    parser.add_argument("--list", action="store_true", help="打印调度表")
    parser.add_argument("--run-once", metavar="KEY", help="立即执行某个任务一次")
    args = parser.parse_args()

    if args.list:
        print(f"{'key':<32} {'cron':<18} {'script':<48} 说明")
        print("-" * 120)
        for key, cron, script, _a, desc in SCHEDULE:
            print(f"{key:<32} {cron:<18} {script:<48} {desc}")
        return 0

    if args.run_once:
        for key, _cron, script, a, _desc in SCHEDULE:
            if key == args.run_once:
                return _run_job(key, script, a)
        print(f"未知任务 key: {args.run_once}（可用 --list 查看）")
        return 1

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TZ)
    enabled = [x.strip() for x in os.getenv("SCHEDULER_ENABLED", "").split(",") if x.strip()]

    scheduler = BlockingScheduler(timezone=tz)
    for key, cron, script, a, desc in SCHEDULE:
        if enabled and key not in enabled:
            print(f"[调度] 跳过（未启用） {key}")
            continue
        scheduler.add_job(
            _run_job,
            CronTrigger.from_crontab(cron, timezone=tz),
            args=[key, script, a],
            id=key,
            name=desc,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        print(f"[调度] 注册 {key:<32} {cron:<18} {script}  ({desc})")

    print("[调度] 启动完成，Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[调度] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
