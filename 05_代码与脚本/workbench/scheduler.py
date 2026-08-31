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

# 把 scripts/src 加入 path（crypto_research 包）
# 容器内路径 /app/scripts/src，本地为 ../../scripts/src
if os.path.exists("/app/scripts/src"):
    _SCRIPTS_SRC = Path("/app/scripts/src")
else:
    _SCRIPTS_SRC = Path(__file__).resolve().parent.parent / "scripts" / "src"
if str(_SCRIPTS_SRC) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_SRC))

from task_manager import (  # noqa: E402
    LOG_DIR,
    STATE_FILE,
    WORKER_SCRIPTS_DIR,
    _append_log,
    _get_db,
    _insert_task,
    _load_task,
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

    # ═══ 链上快照（隔日运行，持仓分布属周级缓慢变化）═══
    # P2-1: 隔日运行以减少 RPC 调用
    ("chain_holder_snapshot_bsc", "30 5 * * 1,3,5", "phase_chain_holder_batch.py", ["--chains", "bsc", "--delay", "0.3", "--timeout", "45"], "链上持仓快照 - BSC 链（周一三五）"),
    ("chain_holder_snapshot_eth", "0 6 * * 2,4,6", "phase_chain_holder_batch.py", ["--chains", "eth", "--delay", "0.3", "--timeout", "45"], "链上持仓快照 - ETH 链（周二四六）"),
    ("chain_holder_snapshot_base_arb", "30 6 * * 1,3,5", "phase_chain_holder_batch.py", ["--chains", "base,arb", "--delay", "0.3", "--timeout", "45"], "链上持仓快照 - Base+Arb 链（周一三五）"),
    ("chain_holder_snapshot_solana", "0 7 * * 2,4,6", "phase_chain_holder_batch.py", ["--chains", "solana", "--delay", "0.5", "--timeout", "60"], "链上持仓快照 - Solana 链（周二四六）"),

    # ═══ 每日数据同步/矫正总调度（串起所有同步/对齐/去重/兜底任务，按依赖顺序执行）═══
    ("data_sync_daily", "30 6 * * *", "run_data_sync_daily.py", [],
     "每日数据同步/矫正总调度（赛道→去重→文档入口→第三方→supply对齐→diff→链接重标→解锁→KOL回测）"),

    # ═══ 每周专项 ═══
    ("third_party_hacks", "30 8 * * 1", "phase_b2_third_party_hacks.py", [], "链上异常事件采集（每周一）"),

    # ═══ 投研数据提取 ═══
    # P1-1: CMC 行情快照降频为每日（长尾币不需要每6h刷新，主流币行情由 ETL 处理）
    ("cmc_quote_snapshot", "0 2 * * *", "ingest_cmc_quote_snapshot.py", ["--top", "10000"], "CMC 行情快照（每日凌晨，覆盖全部 CMC 映射资产）"),
    ("etl_asset_market_daily", "15 */6 * * *", "etl_asset_market_daily_from_cmc.py", [], "行情快照→日级 ETL（每 6 小时，全量回填，CMC 快照后）"),
    ("sync_core_supply", "20 */6 * * *", "sync_core_supply_from_cmc.py", ["--sync"], "主表 supply/市值对齐 CMC（每 6 小时，ETL 后）"),
    # P0-2: 删除 daily_diff_summary 独立调度（已在 data_sync_daily 中运行一次）
    # ("daily_diff_summary", "30 */6 * * *", "daily_diff_generator.py", [], "每日 diff 变化榜（每 6 小时，ETL 后）——已移入 data_sync_daily"),
    ("social_heat_batch", "0 9 * * *", "phase_c_social_heat_batch.py", ["--limit", "500", "--delay", "0.5", "--timeout", "60"], "社交热度批量采集（每日 500 币，跳过稳定币）"),
    ("derivatives_batch", "30 */6 * * *", "phase_derivatives_batch.py", ["--limit", "200", "--delay", "0.2"], "衍生品资金面批量采集（每 6 小时 top 200）"),
    ("tokenomics_extract_batch", "30 9 * * *", "phase_c_extract_tokenomics_auto.py", ["--batch-size", "20", "--max-rounds", "50"], "代币经济学批量提取（每日）"),
    ("whitepaper_summary_extract", "0 10 * * *", "extract_whitepaper_summary.py", ["--all", "--limit", "20"], "白皮书结构化摘要提取（每日 20 份，需 LLM）"),
    ("token_unlocks_batch", "0 10 * * *", "phase_chain_token_unlocks_batch.py", ["--limit", "100", "--delay", "1", "--timeout", "60"], "代币解锁数据采集（每日 100 币）"),
    ("github_activity", "0 11 * * *", "collect_github_activity.py", ["--limit", "50"], "GitHub 仓库活跃度采集（每日 50 个仓库）"),

    # ═══ NotebookLM 精选（默认不启用，需消耗 LLM 配额，手动打开）═══
    # ("notebooklm_curate_batch", "0 12 * * *", "curate_notebooklm.py", ["--batch", "20"], "NotebookLM 精选批量（每日 20 个资产，需 LLM）"),

    # ═══ 文档深度爬取 ═══
    # P1-3: B2 深度文档发现降频为每日（非实时需求）
    ("b2_auto_loop", "0 3 * * *", "phase_b2_auto_loop.py", [], "B2 深度文档发现（每日凌晨 3 点，非实时）"),
    ("spa_browser_crawl_auto", "0 9 * * *", "phase_b2_spa_browser_crawl_auto.py", [], "B3 SPA 无头浏览器爬取"),
    ("b2_ai_noise_clean_by_asset_auto", "0 10 * * *", "phase_b2_ai_noise_clean_by_asset_auto.py", [], "B4 AI 噪声清理（按资产）"),

    # ═══ 监控告警 ═══
    ("chain_transfer_monitor_auto", "*/30 * * * *", "phase_chain_transfer_monitor_auto.py", [], "大额转账监控（跑到完）"),
    ("watchlist_monitor", "*/30 * * * *", "phase_watchlist_monitor.py", [], "解锁/空头/大户监控（单次）"),
    # seed_exchange_wallets 已弃用（2026-08-28），由 collect_exchange_wallets 替代
    # ("seed_exchange_wallets", "0 3 * * 1", "seed_exchange_wallets_auto.py", [], "交易所钱包地址自动采集（每周一）"),
    ("collect_exchange_wallets", "30 3 * * 1", "collect_exchange_wallets.py", ["--chains", "eth,bsc", "--sources", "community,ethplorer", "--apply"], "CEX 地址分级收集-社区源+快照标签（每周一，社区库更新慢）"),

    # ═══ KOL 信号监控（已迁移到 kol_daemon.py 常驻进程，scheduler 不再兜底，避免重复抓取）═══
    # ("kol_monitor_fallback", "*/5 * * * *", "kol_monitor_run.py", ["--run-once"], "KOL 信号监控兜底"),

    # ═══ 催化剂模块（全链路合并任务）═══
    # P1-2: 催化剂降频为每12h（DeepSeek 额度有限）
    ("catalyst_run_all", "0 */12 * * *", "catalyst_run_all.py", [], "催化剂全链路：摄入→AI预处理→thesis重生（每 12 小时）"),

    # ═══ 大盘早报邮件 ═══
    ("daily_brief_email", "0 9 * * *", "send_daily_brief.py", [], "每日大盘早报邮件发送（09:00，在 snapshot 之后）"),

    # ═══ CM / OBM 链上指标定时调度 ═══
    # OBM 数据源活跃但指标慢变（源 CSV 滞后 1 天），降为周一三五；
    # CM MVRV 为实时估值信号，保持每日 T-1 增量。
    ("cm_obm_download", "0 4 * * 1,3,5", "download_obm_data.py", ["--out", "/app/data_external/obm"], "OBM 从 GitHub 下载数据（周一三五 04:00）"),
    ("cm_obm_ingest", "30 4 * * 1,3,5", "ingest_obm_btc_daily.py", [], "OBM BTC 链上指标入库（周一三五 04:30）"),
    ("cm_incremental", "30 6 * * *", "backfill_cm_onchain.py", ["--incremental"], "CM 链上指标 T-1 增量拉取（每日 06:30）"),
    ("cm_validate_onchain", "0 7 * * *", "validate_cm_onchain.py", [], "CM 链上指标入库验证（每日 07:00）"),

    # ═══ 每日早报（P1-4 第二刀：快照落库 + 趋势 diff，早于邮件发送）═══
    ("daily_brief_snapshot", "30 8 * * *", "build_daily_brief.py", [], "每日早报快照落库+趋势diff（Asia/Shanghai 08:30）"),
]


def _build_cmd(script: str, args: list[str]) -> list[str]:
    script_path = str(WORKER_SCRIPTS_DIR / script)
    return [sys.executable, "-u", script_path] + list(args)


def _has_active_task(name_prefix: str) -> bool:
    """检查是否已有同名调度任务在 running/pending 状态（防重叠）。"""
    name_like = f"[调度] {name_prefix}%"
    try:
        with _get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM sys.task
                    WHERE status IN ('running', 'pending')
                      AND name LIKE %s
                    LIMIT 1
                    """,
                    (name_like,),
                )
                return cur.fetchone() is not None
    except Exception as e:
        print(f"[调度] _has_active_task 出错: {e}", file=sys.stderr)
        return False  # 出错时放行，避免调度器彻底停摆


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

    task = {
        "task_id": task_id,
        "name": name,
        "status": "pending",
        "cmd": cmd,
        "started_at": now,
        "ended_at": None,
        "stats": {"scheduler_key": key},
        "error": None,
    }
    try:
        _insert_task(task)
    except Exception as e:
        print(f"[调度] 提交任务失败 {key}: {e}", file=sys.stderr)
        return None

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
    try:
        task_id = submit_scheduled_task(key, script, args, desc)
    except Exception as e:
        print(f"[调度] 提交任务异常 {key}: {e}", file=sys.stderr)
        return
    if not task_id:
        return

    # 轮询等待任务完成（非阻塞调度器线程池，用独立线程等）
    def _wait_and_alert():
        # 最多等 24 小时，超时就不管了
        poll_interval = 10  # 每 10 秒查一次，避免 DB 压力过大
        max_checks = (24 * 3600) // poll_interval
        for _ in range(max_checks):
            time.sleep(poll_interval)
            try:
                task = _load_task(task_id)
            except Exception:
                continue
            if not task:
                return
            status = task.get("status")
            if status in ("done", "failed", "stopped"):
                if status == "failed":
                    # 读最后 50 行日志
                    from task_manager import _read_log
                    try:
                        logs = _read_log(task_id, 50)
                    except Exception:
                        logs = ["(读取日志失败)"]
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
