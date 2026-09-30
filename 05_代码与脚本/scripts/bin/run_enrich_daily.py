#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地「每日地址标签富化」调度启动器 —— Windows 任务计划程序的入口。

为什么必须挂在本机：
    区块浏览器（Etherscan 系列）对服务器 IDC 出口 IP 有 Cloudflare 拦截，
    backfill_enrich_labels.py 的真爬取在云端跑不动，只能在本机（家宽 IP）执行。
    云端 scheduler 里的 enrich_reminder 只负责「提醒积压」，不负责爬。
    所以每日增量富化的执行点必须落在本机，本脚本就是这个执行点的入口。

它解决四件事（都是踩过坑才加的）：
    1. 单实例保护 —— 双重：①扫描命令行含 backfill_enrich_labels.py 的存活 python
       进程；②锁文件兜底。防止与手动跑的任务撞车抢 Etherscan 配额。
       注意只靠锁文件挡不住手动起的进程（手动跑不写锁），所以①是主力。
    2. 无缓冲实时日志 —— 直接 `python xxx.py | grep` 会触发 stdout 块缓冲，
       曾因此误判任务卡死并杀掉正在正常工作的进程。这里用 -u + 逐行 tee。
    3. 日志落盘滚动 —— 每天一个文件，默认保留 14 天。
    4. 结束写 SUMMARY —— 退出码 / 耗时 / 从日志里抓到的新增标签数。

两种模式（同一个脚本、同一把锁，互斥运行）：
    daily（默认）—— 每日全量：清 medium 验证 + 无标签长尾，跑一晚。
    hot          —— 每 30 分钟热跑：只捞最近窗口内的新地址，把
                    「大额转账出现 → 打上标签」的间隔从最坏近 24h 压到 ~30min。

为什么必须两种模式并存：
    热跑解决了「新转账打标太慢」，但清不掉历史长尾；全量解决了长尾，但一天只跑
    一次。只留热跑会让积压永远清不完，只留全量会让页面天天漏报新转账。

用法：
    python run_enrich_daily.py                  # 计划任务调用（默认 daily 全量）
    python run_enrich_daily.py --mode hot       # 热跑（计划任务每 30 分钟调用）
    python run_enrich_daily.py --dry-run        # 只打印将要执行的命令，不真跑
    python run_enrich_daily.py --force          # 忽略锁与进程检测强制执行
    python run_enrich_daily.py --extra-args "--limit 200 --min-count 3"

注册为计划任务（PowerShell，普通权限即可；两个任务共用一把锁，撞车时后到者跳过）：
    $py  = "C:\\Users\\SuperTing\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe"
    $sc  = "E:\\瞎搞乱搞\\web3\\加密货币研究报告\\05_代码与脚本\\scripts"

    # ① 每日全量 21:00
    $act = New-ScheduledTaskAction -Execute $py `
             -Argument "-u `"$sc\\bin\\run_enrich_daily.py`"" -WorkingDirectory $sc
    $trg = New-ScheduledTaskTrigger -Daily -At 21:00
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
             -ExecutionTimeLimit (New-TimeSpan -Hours 10)
    # 超时为什么是 10 小时：带上 --verify-medium 后单轮队列约 1.1 万个地址
    # （medium 待验证 ~6k + 无标签长尾 ~5k），按 0.6~0.9 地址/秒需要 3.5~5 小时；
    # 4 小时会在「长尾」段被硬截断。被截断不丢数据（失败/未跑的地址不入
    # attempt 表，次日自动重试），但白等一晚，故放宽到 10 小时。
    Register-ScheduledTask -TaskName "CryptoOnchainEnrichDaily" -Action $act `
             -Trigger $trg -Settings $set -Force

    # ② 热跑：每 30 分钟一次，每次限时 25 分钟（超时自动结束，下一轮接着来）
    $actH = New-ScheduledTaskAction -Execute $py `
              -Argument "-u `"$sc\\bin\\run_enrich_daily.py`" --mode hot" -WorkingDirectory $sc
    $trgH = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(1) `
              -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
    $setH = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
              -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
    Register-ScheduledTask -TaskName "CryptoOnchainEnrichHot" -Action $actH `
             -Trigger $trgH -Settings $setH -Force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # .../scripts/bin
SCRIPTS_DIR = SCRIPT_DIR.parent                        # .../scripts
TARGET_SCRIPT = SCRIPT_DIR / "backfill_enrich_labels.py"

# 日志与锁放在本机固定目录，不进仓库（避免污染工作区）
LOG_DIR = Path(os.environ.get(
    "ENRICH_LOG_DIR", r"C:/Users/SuperTing/.workbuddy/logs"))
LOCK_FILE = LOG_DIR / "enrich_daily.lock"
LOG_RETENTION_DAYS = 14

# 默认执行参数：全链、min-count=1（只处理未爬过的新地址，天然增量）
#
# 【速率是硬约束，别再往上调】2026-09-30 全量实测：
#   - concurrency 6 / delay 0.4（约 2.1 地址/秒）：跑 12,552 个，
#     从第 2,050 个（16%）开始持续 429，最终 5,174 个失败（41%），
#     失败地址不入 attempt 表 ⇒ 次日原样重爬，白跑一整晚。
#   - concurrency 2 / delay 1.5（约 1.2 地址/秒）：120 个端到端 0 失败。
# 现取更保守的一档（约 0.6~0.9 地址/秒），宁可慢也不要把 IP 打进惩罚窗口——
# 一旦被惩罚，后续数小时即使降速仍只有 ~0.5/秒的有效产出，得不偿失。
#
# 【--verify-medium 为什么必须带上】2026-09-30：
#   库里 19,352 条 confidence='medium' 全部是 evm_propagate 的跨链传播副本，
#   它们「有标签」所以永远进不了富化队列，从未被浏览器真实验证过。
#   带上本开关后进入队列，命中即原地升级 high（实测 30 个命中 21 个，70%，
#   远高于散户长尾的 ~2%），是投入产出比最高的一批地址。
DEFAULT_ARGS = [
    "--chain", "eth,base,polygon",
    "--min-count", "1",
    "--limit", "0",
    "--verify-medium",
    "--concurrency", "2",
    "--delay", "2.0",
]

# 热跑参数（--mode hot）。三处与全量的差异都是刻意的：
#   --since-hours 4：限定「最近 4 小时出现过的地址」。不带窗口时候选池按全库频次
#                    排序，历史长尾会把 --limit 占满，新转账地址根本挤不进队列
#                    ——页面就会持续漏报当前窗口的大额转账。
#   --limit 150    ：单轮上限，按 0.6~0.9 地址/秒约 3~4 分钟跑完，不会占用到
#                    下一轮（30 分钟）的开跑时刻；超时也只影响本轮，下轮接着来。
#   不带 --verify-medium：medium 验证是「一次性清历史库存」的工作，属于每日全量
#                    的范畴；热跑只做新地址增量，避免把配额花在重复验证上。
HOT_ARGS = [
    "--chain", "eth,base,polygon",
    "--min-count", "1",
    "--limit", "150",
    "--since-hours", "4",
    "--concurrency", "2",
    "--delay", "2.0",
]

MODE_ARGS = {"daily": DEFAULT_ARGS, "hot": HOT_ARGS}

# 每日全量的触发时刻（与上面注册命令保持一致）。热跑在这个时刻前后
# HOT_DAILY_GUARD_MIN 分钟内直接跳过：两者抢同一把锁，若热跑恰好先拿到锁，
# 当日全量就会被「已有实例」挡掉一整天（单实例保护在热跑里同样生效）。
DAILY_TRIGGER_HOUR = 21
HOT_DAILY_GUARD_MIN = 15

# 从进度行里抓「已查到标签 N 个」用于 SUMMARY
LABEL_RE = re.compile(r"已查到标签\s*(\d+)\s*个")
# 抓「升级 medium→high N 条」—— medium 验证的核心产出，值得单列
UPGRADE_RE = re.compile(r"升级 medium→high\s*(\d+)\s*条")
# 从批次行里抓「失败: 429×N」用于限流熔断
FAIL429_RE = re.compile(r"429[×x](\d+)")

# 熔断阈值：一批 50 个里挂掉 >=25 个算「大面积 429」，连续这么多批就停
BAD_BATCH_429 = 25
BAD_BATCH_STREAK = 6


def _log(msg: str) -> None:
    print(msg, flush=True)


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否存活。查不到时保守返回 True（宁可不跑，也不并发抢配额）。"""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, timeout=15)
        txt = out.stdout.decode("utf-8", errors="replace")
    except Exception:
        return True
    return str(pid) in txt


def find_running_enrich_pids() -> list[int]:
    """找出命令行含 backfill_enrich_labels.py 的存活 python 进程（排除自己）。"""
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*backfill_enrich_labels.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, timeout=45)
        raw = out.stdout.decode("utf-8", errors="replace")
    except Exception as e:                      # CIM 查不动 → 交回锁文件兜底判断
        _log(f"  ⚠️  进程扫描失败({type(e).__name__})，仅依赖锁文件判断")
        return []
    me = os.getpid()
    pids = []
    for tok in raw.split():
        tok = tok.strip()
        if tok.isdigit():
            pid = int(tok)
            if pid != me:
                pids.append(pid)
    return pids


def acquire_lock(force: bool) -> bool:
    """写锁文件。返回 True 表示取得锁。"""
    if LOCK_FILE.exists():
        try:
            info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            old_pid = int(info.get("pid", 0))
        except Exception:
            old_pid = 0
        if _pid_alive(old_pid) and not force:
            _log(f"  ⛔ 已有实例在跑（锁文件 PID={old_pid}，"
                 f"启动于 {info.get('started_at')}），本次跳过")
            return False
        _log(f"  🧹 发现陈旧锁（PID={old_pid} 已退出），清理后继续")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": str(TARGET_SCRIPT),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


def _in_daily_guard(now: datetime) -> bool:
    """热跑是否落在每日全量的前后保护窗口内（用于让位）。"""
    start = now.replace(hour=DAILY_TRIGGER_HOUR, minute=0, second=0, microsecond=0)
    return abs((now - start).total_seconds()) <= HOT_DAILY_GUARD_MIN * 60


def clean_old_logs() -> None:
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    for f in LOG_DIR.glob("enrich_*.log"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def main() -> int:
    global LOG_DIR, LOCK_FILE          # 必须在任何引用之前声明

    parser = argparse.ArgumentParser(
        description="本地地址标签富化调度启动器（单实例 + 实时日志）")
    parser.add_argument("--mode", choices=("daily", "hot"), default="daily",
                        help="daily=每日全量（清 medium + 长尾）；"
                             "hot=每 30 分钟热跑（只捞最近 4h 新地址）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的命令，不真正启动")
    parser.add_argument("--force", action="store_true",
                        help="忽略锁文件与进程检测，强制执行")
    parser.add_argument("--extra-args", type=str, default="",
                        help="追加/覆盖给 backfill_enrich_labels.py 的参数"
                             "（放在默认参数之后，同名参数以它为准）")
    parser.add_argument("--log-dir", type=str, default=None,
                        help=f"日志目录（默认 {LOG_DIR}）")
    args = parser.parse_args()

    if args.log_dir:
        LOG_DIR = Path(args.log_dir)
        LOCK_FILE = LOG_DIR / "enrich_daily.lock"

    mode = args.mode
    mode_txt = "热跑" if mode == "hot" else "每日全量"
    cmd = [sys.executable, "-u", str(TARGET_SCRIPT), *MODE_ARGS[mode]]
    if args.extra_args.strip():
        cmd += args.extra_args.strip().split()

    _log("=" * 68)
    _log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 地址标签富化（{mode_txt}）")
    _log(f"  工作目录: {SCRIPTS_DIR}")
    _log(f"  解释器  : {sys.executable}")
    _log(f"  命令    : {' '.join(cmd)}")

    if args.dry_run:
        _log("  [dry-run] 未实际执行")
        return 0

    # 热跑避开每日全量的触发时刻：两者共用一把锁，热跑若先抢到锁会让当日全量
    # 被单实例保护挡掉，长尾与 medium 验证就整整一天不跑。
    if mode == "hot" and not args.force and _in_daily_guard(datetime.now()):
        _log(f"  ⏸ 距每日全量（{DAILY_TRIGGER_HOUR:02d}:00）不足 "
             f"{HOT_DAILY_GUARD_MIN} 分钟，本次热跑让位跳过")
        return 0

    # 日志文件必须在任何分支判断之前打开：
    # 计划任务下控制台输出会丢弃，若「跳过」不留痕，事后无法区分
    # 「任务没跑」和「跑了但被单实例保护跳过」——踩过这个坑。
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 两种模式分开记日志：热跑一天 48 次，混进全量日志里会把一夜的长跑日志淹掉
    log_path = LOG_DIR / f"enrich_{'hot' if mode == 'hot' else 'daily'}_{datetime.now():%Y%m%d}.log"
    t0 = time.time()
    last_label_n = 0
    last_upgrade_n = 0
    exit_code = -1

    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            def emit(msg: str) -> None:
                """控制台 + 日志文件双写，任何分支都留痕。"""
                print(msg, flush=True)
                lf.write(msg.rstrip("\n") + "\n")
                lf.flush()

            emit(f"\n{'=' * 60}\n"
                 f"[{datetime.now():%Y-%m-%d %H:%M:%S}] START\n"
                 f"CMD: {' '.join(cmd)}\n{'=' * 60}")

            # --- 单实例保护 ---
            running = find_running_enrich_pids()
            if running and not args.force:
                emit(f"  ⛔ 检测到 {len(running)} 个正在运行的富化进程 "
                     f"(PID={running})，本次跳过以避免抢配额")
                emit("     若确认是残留，加 --force 强制执行")
                emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] END "
                     f"exit=0 耗时={(time.time() - t0) / 60:.1f}分钟 "
                     f"结果=跳过(已有实例)")
                return 0

            if not acquire_lock(force=args.force):
                emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] END "
                     f"exit=0 结果=跳过(未取得锁)")
                return 0

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"       # 保证子进程输出 UTF-8
            env["PYTHONUNBUFFERED"] = "1"
            # 本机直连 prod，不走代理（代理会拦或降速）
            for p in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                      "https_proxy", "ALL_PROXY", "all_proxy"):
                env.pop(p, None)

            # 【不要改回 stdout=PIPE 实时读】2026-09-30 实测踩坑：
            # 在 Windows 计划任务会话下，Popen(stdout=PIPE) + 逐行读的模式
            # 子进程输出会被无限期憋住（14 分钟一个字节都到不了父进程，
            # 进程被杀后缓冲才一次性吐出）——同脚本交互终端下完全实时。
            # 熔断器因此全瞎，且日志文件 14 分钟不增长，极易误判卡死被杀。
            # 现改为：子进程 stdout 直接绑日志文件（绕开 PIPE），
            # 父进程定期 seek 读文件尾部做 429 熔断 + 计数提取。
            # 子进程 stdout 直接绑日志文件（见下方 Popen 注释）
            log_handle = lf

            proc = subprocess.Popen(
                cmd, cwd=str(SCRIPTS_DIR), stdout=log_handle,
                stderr=subprocess.STDOUT, env=env)

            file_offset = 0
            try:
                file_offset = log_handle.tell()
            except Exception:
                file_offset = 0

            bad_streak = 0
            halted_by_429 = False
            last_progress_print = ""
            while proc.poll() is None:
                time.sleep(30)
                # 读日志文件增量（二进制 seek 避免文本模式 tell 的 cookie 问题）
                try:
                    with open(log_path, "rb") as rf:
                        rf.seek(file_offset)
                        chunk = rf.read()
                        file_offset = rf.tell()
                except OSError:
                    continue
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    m = LABEL_RE.search(line)
                    if m:
                        last_label_n = int(m.group(1))
                    mu = UPGRADE_RE.search(line)
                    if mu:
                        last_upgrade_n += int(mu.group(1))

                    # 429 熔断（从文件增量里检测，与旧 PIPE 逻辑同阈值）
                    if "批次完成" in line:
                        last_progress_print = line.strip()
                        m429 = FAIL429_RE.search(line)
                        n429 = int(m429.group(1)) if m429 else 0
                        if n429 >= BAD_BATCH_429:
                            bad_streak += 1
                        else:
                            bad_streak = 0
                        if bad_streak >= BAD_BATCH_STREAK:
                            emit(f"  🛑 连续 {bad_streak} 批大面积 429（每批 "
                                 f"≥{BAD_BATCH_429} 个失败），判定 IP 已进入限流"
                                 f"惩罚窗口，主动中止本次运行")
                            emit("     已爬到的标签已入库；失败地址未记账，明日自动重试")
                            proc.terminate()
                            halted_by_429 = True
                            break
                if halted_by_429:
                    break
                # 每 30 秒打一行最新批次进度（控制台可观测）
                if last_progress_print:
                    emit(f"  [watch] {last_progress_print}")

            try:
                exit_code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait()

            elapsed = time.time() - t0
            tail = f"（因限流熔断中止，已处理约 {bad_streak * 50} 个的最后窗口）" \
                if halted_by_429 else ""
            summary = (f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] END "
                       f"exit={exit_code} 耗时={elapsed / 60:.1f}分钟 "
                       f"新增标签≈{last_label_n} "
                       f"medium→high={last_upgrade_n}{tail}\n")
            lf.write(summary)
            _log(summary.strip())
    finally:
        release_lock()
        clean_old_logs()

    _log(f"  日志: {log_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
