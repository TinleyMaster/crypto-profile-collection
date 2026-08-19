"""
定时调度器：用 APScheduler 按 cron 表达式触发采集任务，提交到 TaskManager 统一执行。

设计：
  - 调度器只负责「到点提交任务」，不直接执行脚本
  - 任务执行、日志、状态全由 TaskManager 统一管理（工作台可查）
  - 同一任务不重叠：提交前检查是否已有同名任务在 running/pending，有则跳过
  - 失败告警：SMTP 邮件（复用 SMTP_* 环境变量）

用法：
    python scheduler.py                 # 前台常驻
    python scheduler.py --list          # 打印调度表
    python scheduler.py --run-once <key>  # 立即提交某个任务一次（测试/补跑）

环境变量（均可选）：
    SCHEDULER_TIMEZONE  时区（默认 Asia/Shanghai）
    SCHEDULER_ENABLED   逗号分隔的 key 白名单，空 = 全部启用
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_TO / SMTP_FROM  失败邮件告警
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 复用 task_manager 的状态/日志路径和文件锁工具
if os.path.exists("/app/scripts/bin"):
    _DEFAULT_STATE_DIR = Path("/app/task_state")
else:
    _DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "task_state"

STATE_DIR = Path(os.getenv("SCHEDULER_LOG_DIR") or _DEFAULT_STATE_DIR)
STATE_DIR.mkdir(parents=True, exist_ok=True)

# 把 workbench 目录加入 path，直接 import task_manager 的工具函数
sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_manager import (  # noqa: E402
    LOG_DIR,
    STATE_FILE,
    WORKER_SCRIPTS_DIR,
    _append_log,
    _load_state,
    _save_state,
    _lock,
)

import uuid  # noqa: E402

TZ = os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")

# 调度表：(key, cron, script, args, 说明)
# cron 为 5 段式（分 时 日 月 周）。
SCHEDULE: list[tuple[str, str, str, list[str], str]] = [
    # ═══ 数据源流水线 ═══
    ("cmc_pipeline", "0 3 * * *", "run_cmc_pipeline.py", [], "CMC 一键流水线（每日）"),
    ("dl_pipeline", "0 4 * * *", "run_dl_pipeline.py", [], "DefiLlama 一键流水线（每日）"),
    ("cg_pipeline", "0 5 * * 1", "run_cg_pipeline.py", [], "CoinGecko 一键流水线（每周一，月配额 10k）"),

    # ═══ 链上快照（每日全量）═══
    ("chain_holder_snapshot_bsc", "30 5 * * *", "phase_chain_holder_batch.py", ["--chains", "bsc", "--delay", "0.3", "--timeout", "45"], "链上持仓快照 - BSC 链（每日）"),
    ("chain_holder_snapshot_eth", "0 6 * * *", "phase_chain_holder_batch.py", ["--chains", "eth", "--delay", "0.3", "--timeout", "45"], "链上持仓快照 - ETH 链（每日）"),
    ("chain_holder_snapshot_base_arb", "30 6 * * *", "phase_chain_holder_batch.py", ["--chains", "base,arb", "--delay", "0.3", "--timeout", "45"], "链上持仓快照 - Base+Arb 链（每日）"),
    ("chain_holder_snapshot_solana", "0 7 * * *", "phase_chain_holder_batch.py", ["--chains", "solana", "--delay", "0.5", "--timeout", "60"], "链上持仓快照 - Solana 链（每日）"),

    # ═══ 赛道分类刷新 ═══
    ("refresh_sectors", "0 6 * * *", "run_refresh_sectors.py", [], "赛道标签全量刷新（每日兜底）"),

    # ═══ 官网 primary 裁决 ═══
    ("refresh_primary_website", "30 6 * * *", "run_refresh_primary_website.py", [], "官网 primary 裁决（每日兜底）"),

    # ═══ 文档入口补充（自动循环）═══
    ("cmc_refresh_docs_auto", "0 6 * * *", "refresh_doc_source_entries_from_cmc_auto.py", [], "CMC 补充文档入口"),
    ("cg_refresh_docs_auto", "0 6 * * *", "refresh_doc_source_entries_from_cg_auto.py", [], "CG 补充文档入口"),
    ("dl_refresh_docs_auto", "0 6 * * *", "refresh_doc_source_entries_from_dl_auto.py", [], "DL 补充文档入口"),
    ("dual_supplement_auto", "0 7 * * *", "supplement_doc_entries_dual_auto.py", [], "双源(DexScreener+Binance)补充文档入口"),

    # ═══ 第三方专项 ═══
    ("third_party_auto", "0 8 * * *", "phase_b2_third_party_auto.py", [], "第三方评级/审计回填"),
    ("third_party_raises_auto", "0 8 * * *", "phase_b2_third_party_raises_auto.py", [], "TGE/融资轮次采集"),
    ("third_party_hacks", "30 8 * * 1", "phase_b2_third_party_hacks.py", [], "链上异常事件采集（每周一）"),

    # ═══ 投研数据提取 ═══
    ("cmc_quote_snapshot", "0 */6 * * *", "ingest_cmc_quote_snapshot.py", ["--top", "1000"], "CMC 行情快照（每 6 小时 top 1000）"),
    ("social_heat_batch", "0 9 * * *", "phase_c_social_heat_batch.py", ["--limit", "500", "--delay", "0.5", "--timeout", "60"], "社交热度批量采集（每日 500 币）"),
    ("tokenomics_extract_batch", "30 9 * * *", "phase_c_extract_tokenomics_auto.py", ["--batch-size", "20", "--max-rounds", "50"], "代币经济学批量提取（每日）"),
    ("token_unlocks_batch", "0 10 * * *", "phase_chain_token_unlocks_batch.py", ["--limit", "100", "--delay", "1", "--timeout", "60"], "代币解锁数据采集（每日 100 币）"),

    # ═══ 文档深度爬取 ═══
    ("b2_auto_loop", "0 */6 * * *", "phase_b2_auto_loop.py", [], "B2 深度文档发现（每 6 小时）"),
    ("spa_browser_crawl_auto", "0 9 * * *", "phase_b2_spa_browser_crawl_auto.py", [], "B3 SPA 无头浏览器爬取"),
    ("b2_ai_noise_clean_by_asset_auto", "0 10 * * *", "phase_b2_ai_noise_clean_by_asset_auto.py", [], "B4 AI 噪声清理（按资产）"),

    # ═══ 监控告警 ═══
    ("chain_transfer_monitor_auto", "*/30 * * * *", "phase_chain_transfer_monitor_auto.py", [], "大额转账监控（跑到完）"),
    ("watchlist_monitor", "*/30 * * * *", "phase_watchlist_monitor.py", [], "解锁/空头/大户监控（单次）"),
]


def _build_cmd(script: str, args: list[str]) -> list[str]:
    script_path = str(WORKER_SCRIPTS_DIR / script)
    return [sys.executable, "-u", script_path] + list(args)


def _has_active_task(name_prefix: str) -> bool:
    """检查是否已有同名调度任务在 running/pending 状态（防重叠）。"""
    with _lock():
        state = _load_state()
    for t in state["tasks"].values():
        if t.get("status") in ("running", "pending") and t.get("name", "").startswith(f"[调度] {name_prefix}"):
            return True
    return False


def submit_scheduled_task(key: str, script: str, args: list[str], desc: str) -> str | None:
    """提交一个调度任务到 TaskManager 队列。返回 task_id 或 None（被去重跳过）。"""
    name = f"[调度] {key} - {desc}"

    # 防重叠：已有同名任务在跑就跳过
    if _has_active_task(key):
        print(f"[调度] {key} 已有任务在执行，跳过本次触发")
        return None

    task_id = uuid.uuid4().hex[:12]
    cmd = _build_cmd(script, args)
    now = time.time()

    with _lock():
        state = _load_state()
        state["tasks"][task_id] = {
            "task_id": task_id,
            "name": name,
            "status": "pending",
            "cmd": cmd,
            "started_at": now,
            "ended_at": None,
            "stats": {"scheduler_key": key},
            "error": None,
        }
        state["pending"].append(task_id)
        _save_state(state)

    _append_log(task_id, f"[TASK] 调度触发: {key}")
    _append_log(task_id, f"[TASK] CMD: {' '.join(cmd)}")
    print(f"[调度] 已提交 {key} -> task_id={task_id}")
    return task_id


def _send_alert_email(subject: str, body_text: str) -> bool:
    """失败告警邮件（自包含 smtplib）。"""
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    pwd = os.getenv("SMTP_PASS", "")
    to = os.getenv("SMTP_TO", "").strip()
    if not (host and user and pwd and to):
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
        print(f"[调度] 失败告警邮件已发送: {subject}")
        return True
    except Exception as e:
        print(f"[调度] 告警邮件发送失败: {e}")
        return False


def _scheduler_job(key: str, script: str, args: list[str], desc: str) -> None:
    """APScheduler 回调：提交任务 + 异步等待完成 + 失败发邮件。"""
    task_id = submit_scheduled_task(key, script, args, desc)
    if not task_id:
        return

    # 轮询等待任务完成（非阻塞调度器线程池，用独立线程等）
    def _wait_and_alert():
        # 最多等 24 小时，超时就不管了
        for _ in range(24 * 3600):  # 每秒检查一次
            time.sleep(1)
            with _lock():
                state = _load_state()
            task = state["tasks"].get(task_id)
            if not task:
                return
            status = task.get("status")
            if status in ("done", "failed", "stopped"):
                if status == "failed":
                    # 读最后 50 行日志
                    from task_manager import _read_log
                    logs = _read_log(task_id, 50)
                    body = (
                        f"任务：{key}\n"
                        f"task_id：{task_id}\n"
                        f"状态：{status}\n"
                        f"错误：{task.get('error') or '未知'}\n\n"
                        f"最后 50 行日志：\n" + "\n".join(logs)
                    )
                    _send_alert_email(
                        f"⚠️ [调度] {key} 执行失败",
                        body,
                    )
                return

    import threading
    threading.Thread(target=_wait_and_alert, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="定时调度器（任务提交到 TaskManager）")
    parser.add_argument("--list", action="store_true", help="打印调度表")
    parser.add_argument("--run-once", metavar="KEY", help="立即提交某个任务一次")
    args = parser.parse_args()

    if args.list:
        print(f"{'key':<32} {'cron':<18} {'script':<48} 说明")
        print("-" * 120)
        for key, cron, script, _a, desc in SCHEDULE:
            print(f"{key:<32} {cron:<18} {script:<48} {desc}")
        print(f"\n状态文件: {STATE_FILE}")
        print(f"日志目录: {LOG_DIR}")
        return 0

    if args.run_once:
        for key, _cron, script, a, desc in SCHEDULE:
            if key == args.run_once:
                task_id = submit_scheduled_task(key, script, a, desc)
                if task_id:
                    print(f"已提交: task_id={task_id}")
                return 0
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
            _scheduler_job,
            CronTrigger.from_crontab(cron, timezone=tz),
            args=[key, script, a, desc],
            id=key,
            name=desc,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        print(f"[调度] 注册 {key:<32} {cron:<18} {script}  ({desc})")

    print(f"[调度] 启动完成，状态文件: {STATE_FILE}")
    print(f"[调度] 任务提交到 TaskManager pending 队列，由 Flask 进程的 runner 执行")
    print("[调度] Ctrl+C 退出")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[调度] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
