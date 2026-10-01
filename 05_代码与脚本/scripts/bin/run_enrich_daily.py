#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地「地址标签富化」调度启动器 —— Windows 任务计划程序 / macOS launchd 的入口。

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

三种模式（同一个脚本、同一把锁，互斥运行）：
    daily（默认）—— 每日全量：清 medium 验证 + 无标签长尾，跑一晚。
    hot          —— 每 30 分钟热跑：只捞最近 4h 窗口内的新地址，把
                    「大额转账出现 → 打上标签」的间隔从最坏近 24h 压到 ~30min。
    backlog      —— 每 30 分钟清积压：不限时间窗，按 whale 候选榜 + 频次从全库
                    长尾里取（每链 --limit 400，约 13 分钟跑完），把历史积压慢慢啃完。

为什么必须三种模式并存：
    热跑解决了「新转账打标太慢」，但清不掉历史长尾；全量解决了长尾，但一天只跑
    一次且上限 3h；积压若上万，光靠一天一次全量清不完，页面会长期显示大量 n/a。
    故 30 分钟档清积压啃长尾、每日全量清 medium 验证，两者互补。

用法：
    python run_enrich_daily.py                  # 计划任务调用（默认 daily 全量）
    python run_enrich_daily.py --mode hot       # 热跑（计划任务每 30 分钟调用）
    python run_enrich_daily.py --mode backlog   # 清积压（计划任务每 30 分钟调用）
    python run_enrich_daily.py --dry-run        # 只打印将要执行的命令，不真跑
    python run_enrich_daily.py --force          # 忽略锁与进程检测强制执行
    python run_enrich_daily.py --extra-args "--limit 200 --min-count 3"

注册为计划任务（PowerShell，普通权限即可；两个任务共用一把锁，撞车时后到者跳过）：
    解释器固定用 C:\\python\\python311\\python.exe —— 已注册的 Daily 任务用的就是它，
    换解释器会让两个任务在命令行的进程扫描口径上不一致。

    $py  = "C:\\python\\python311\\python.exe"
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
    # 注意 -Daily 与 -RepetitionInterval 在 New-ScheduledTaskTrigger 里属于不同的
    # 参数集，会报 "Parameter set cannot be resolved"；必须写成 -Once + 重复。
    $actH = New-ScheduledTaskAction -Execute $py `
              -Argument "-u `"$sc\\bin\\run_enrich_daily.py`" --mode hot" -WorkingDirectory $sc
    $trgH = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(7) `
              -RepetitionInterval (New-TimeSpan -Minutes 30) `
              -RepetitionDuration (New-TimeSpan -Days 3650)
    $setH = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
              -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
    Register-ScheduledTask -TaskName "CryptoOnchainEnrichHot" -Action $actH `
             -Trigger $trgH -Settings $setH -Force

注册为计划任务（macOS / launchd，2026-10-01 移植）：
    本脚本已跨平台：日志目录、进程扫描、存活判断都按 os.name 分支（POSIX 用
    pgrep -f + ps + os.kill(pid,0)，Windows 沿用 PowerShell + tasklist）。
    launchd 没有 Windows 计划任务的 ExecutionTimeLimit，故整体运行上限改由脚本内的
    MAX_RUNTIME_MIN 兜底（可用环境变量 ENRICH_MAX_RUNTIME_MIN 覆盖）。

    两个 LaunchAgent 各写一个 plist 到 ~/Library/LaunchAgents/（属主须为本人、
    不可 group/world 可写，否则 launchd 报 bad ownership/permissions；路径一律写绝对
    路径，plist 不展开 ~）：

    com.tinley.crypto.enrich.backlog.plist  —— 每 30 分钟清积压长尾
        ProgramArguments = [<repo>/.venv/bin/python, -u,
                            <repo>/05_代码与脚本/scripts/bin/run_enrich_daily.py,
                            --mode, backlog]
        StartInterval    = 1800
        RunAtLoad        = true      （登录/重启后立刻补一轮）
        KeepAlive        = false     （true 会跑完立即重启，打乱节拍冲配额）
        ProcessType      = Background
        WorkingDirectory = <repo>/05_代码与脚本/scripts
        StandardOutPath  = <HOME>/.workbuddy/logs/launchd_enrich_backlog.out
        StandardErrorPath= <HOME>/.workbuddy/logs/launchd_enrich_backlog.err
        EnvironmentVariables = { PATH, HOME, LANG=zh_CN.UTF-8, TZ=Asia/Shanghai,
                                 PYTHONUNBUFFERED=1, PYTHONIOENCODING=utf-8,
                                 ENRICH_LOG_DIR=<HOME>/.workbuddy/logs }
        （不要把 DATABASE_URL 写进 plist——config.py 直接读 scripts/.env，与 cwd 无关）

    com.tinley.crypto.enrich.daily.plist    —— 每日 09:00 清 medium 验证 + 长尾
        同上，但 --mode daily、StartCalendarInterval = {Hour 9, Minute 0}、
        RunAtLoad = false（避免登录即跑数小时）、日志文件后缀改 daily。

    装载（macOS 15 已废弃 load -w / unload；改 plist 后必须 bootout → bootstrap）：
        plutil -lint ~/Library/LaunchAgents/com.tinley.crypto.enrich.backlog.plist
        launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tinley.crypto.enrich.backlog.plist
        launchctl enable   gui/$(id -u)/com.tinley.crypto.enrich.backlog
        launchctl print    gui/$(id -u)/com.tinley.crypto.enrich.backlog
        launchctl kickstart -k gui/$(id -u)/com.tinley.crypto.enrich.backlog   # 立即触发一轮
        launchctl bootout  gui/$(id -u)/com.tinley.crypto.enrich.backlog       # 卸载

    睡眠/唤醒：【这条 2026-10-01 实测纠正过，原文写错了】StartInterval 在睡眠期间
    根本不计时，醒来也不会补拍 —— 当天的日志很清楚：16:39 跑完后机器 16:40 进入
    Deep Idle，到 18:08 唤醒，本该 18:09:29 触发的那一拍并没有触发，中间整整 90
    分钟一次都没跑。所以「每 30 分钟一轮」的前提是「机器这 30 分钟得醒着」。本机
    是电池供电的笔记本，空闲几分钟就睡，实测白天 8 轮的真实间隔是 41/56/55/89
    分钟。对策见下面「已知边界」第一条。
    另外，launchd 不会为了跑任务把机器从睡眠中唤醒（WakeSystem 仅 LaunchDaemon 可用）。

已知边界：
    - 【每 30 分钟一轮的真实前提】机器醒着。本脚本为此做两件事：
      ① 单轮上限收到 MAX_RUNTIME_MIN["backlog"/"hot"]=15 分钟、backlog 队列收到
         --limit 400，保证一轮在每一拍内干净跑完。上限若贴着 30 分钟，收尾会顶到
         下一拍、被 StartInterval 整拍跳过，实测节拍漂到 55~60 分钟。
      ② 运行期用 caffeinate -i 拦空闲休眠（见 _start_keepawake），避免跑到一半
         被睡冻住、白耗到上限才被杀。合盖/主动睡眠仍然会睡，只是下一拍不会补。
      如果哪天要「合上盖子也每 30 分钟跑」，只能装 root LaunchDaemon 配
      StartCalendarInterval + WakeSystem 定时唤醒，代价是明显的电池消耗。
    - 每日全量跑起来期间，热跑/清积压每一轮都会被单实例保护跳过；
      这是刻意的——它们共用 Etherscan 配额，抢跑只会把 IP 打进限流惩罚窗口。
      故 daily 的运行上限收在 MAX_RUNTIME_MIN["daily"]=3h（不是 Windows 的 10h）：
      它持锁期间 30 分钟档全停，3h 足够做完 medium 验证的主体，又不至于把
      「清积压长尾」这个主目标饿死一整个工作日。
    - 热跑与清积压都在 09:00（DAILY_TRIGGER_HOUR）前后 HOT_DAILY_GUARD_MIN 分钟内
      主动让位，否则它若先拿到锁，当日全量会被跳过一整天。
    - 子进程若卡死，靠 MAX_RUNTIME_MIN 到点强杀；SIGKILL/断电残留的陈旧锁
      由下一次的 PID 存活判断清理。
    - 出口 IP 被 Cloudflare 拦（每批大面积 403）时除了熔断中止，还会发邮件提醒
      换代理节点（只发 ADMIN_EMAIL，未配置回退 SMTP_TO），6 小时内最多一封
      （ENRICH_IP_ALERT_COOLDOWN_H 可覆盖），换回干净出口后补发一封「已恢复」。
      判定阈值 BAD_BATCH_STREAK_403=3：连续 3 批每批 403 ≥25 个。403 与 429 分开
      计数（429 会给 6 批的宽限，因为它可能自愈）。403 失败的地址不写 attempt 表，
      换节点后自动续跑，积压不会被刷成假「已尝试」。告警状态文件
      enrich_ip_alert.state 落在日志目录，删除即重置冷却。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # .../scripts/bin
SCRIPTS_DIR = SCRIPT_DIR.parent                        # .../scripts
SRC_DIR = SCRIPTS_DIR / "src"                          # .../scripts/src
TARGET_SCRIPT = SCRIPT_DIR / "backfill_enrich_labels.py"

# 日志与锁放在本机固定目录，不进仓库（避免污染工作区）
# 2026-10-01 跨平台：POSIX 走家目录，Windows 保留原字面路径；ENRICH_LOG_DIR 始终可覆盖。
_DEFAULT_LOG_DIR = (Path.home() / ".workbuddy" / "logs") if os.name == "posix" \
    else Path(r"C:/Users/SuperTing/.workbuddy/logs")
LOG_DIR = Path(os.environ.get("ENRICH_LOG_DIR", str(_DEFAULT_LOG_DIR)))
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
    "--whale-priority",
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

# 清积压参数（--mode backlog，2026-10-01 macOS 新增）。与热跑的唯一差别：
#   不带 --since-hours ⇒ 不限时间窗，队列按 whale 候选榜 + 频次从全库积压里取，
#   这才是「把 3 万个历史长尾啃完」的口径；热跑只补最近 4h 的新地址、不减积压。
#   --limit 400 是「每条链」上限（见 backfill_enrich_labels.py 的 --limit 说明）。
#   【为什么从 900 降到 400】2026-10-01 实测：--limit 900 时单轮实际约 700 个
#   地址、耗时 18 分钟，加上收尾接近 25 分钟 —— 几乎等于 30 分钟的节拍。
#   launchd 的 StartInterval 一旦在「上一轮还在跑」时到点，那一拍就被整拍跳过，
#   实际节拍漂到 55~60 分钟。压到 400（约 500 个地址、13 分钟）才能在每一拍内
#   干净跑完，真正拿到 30 分钟一轮；每轮少爬的部分由「轮数翻倍」补回来。
BACKLOG_ARGS = [
    "--chain", "eth,base,polygon",
    "--min-count", "1",
    "--limit", "400",
    "--whale-priority",
    "--concurrency", "2",
    "--delay", "2.0",
]

MODE_ARGS = {"daily": DEFAULT_ARGS, "hot": HOT_ARGS, "backlog": BACKLOG_ARGS}

# 整体运行上限（分钟）。launchd 没有 Windows 计划任务的 ExecutionTimeLimit 等价项：
# 子进程若卡死（网络黑洞 / explorer 挂起）会永久占锁，之后每一轮都被单实例保护跳过，
# 页面标签年龄突破 workbench/onchain_alert.py 的 LABEL_STALE_HOURS=26h（历史上真发生过
# 11 天断供）。到点强杀即可，未爬的地址不入 attempt 表，下一轮自动续跑。
# daily 收在 3h（Windows 原为 10h）：它持锁期间 30 分钟档全停，3h 后必须把锁还回去。
# backlog/hot 从 25 收到 15：上限必须明显小于 30 分钟的节拍，否则单轮收尾会顶到
# 下一拍、导致那一拍被 StartInterval 整拍跳过（详见 BACKLOG_ARGS 上方注释）。
MAX_RUNTIME_MIN = {"daily": 180, "hot": 15, "backlog": 15}

# 每日全量的触发时刻（与上面注册命令保持一致）。热跑在这个时刻前后
# HOT_DAILY_GUARD_MIN 分钟内直接跳过：两者抢同一把锁，若热跑恰好先拿到锁，
# 当日全量就会被「已有实例」挡掉一整天（单实例保护在热跑里同样生效）。
# 2026-09-30 改 9：工作电脑只有工作日开机、21:00 已关机 ⇒ 全量改到 09:00
# 触发 + StartWhenAvailable 开机补跑；热跑在 09:00 前后让位。
DAILY_TRIGGER_HOUR = 9
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

# 403 熔断（2026-10-01 新增，判「出口 IP 被 Cloudflare 拦」）。
# 与 429 性质不同：429 是「爬太快被限流」，等一等会自愈；403 是「这条线路的
# 出口 IP 被判定为机房/风险 IP」，实测连 Playwright 真实浏览器兜底也过不去，
# 只能人工换节点，所以除了熔断还要发邮件提醒。
# 实测：坏 IP 时每批固定 403×50（100% 失败），换回干净出口立刻恢复 200，
# 故阈值取「连续 3 批每批 ≥25 个 403」，约 1~3 分钟即可判定。
FAIL403_RE = re.compile(r"403[×x](\d+)")
BAD_BATCH_403 = 25
BAD_BATCH_STREAK_403 = 3

# IP 告警去重：坏 IP 期间每轮都会撞 403，不能每 30 分钟发一封信。
# 状态文件记「上次发信时刻」+「当前是否处于坏 IP 状态」，恢复时据此补发
# 一封「已恢复」。ENRICH_IP_ALERT_COOLDOWN_H 可覆盖冷却小时数（测试用）。
IP_ALERT_COOLDOWN_H = float(os.environ.get("ENRICH_IP_ALERT_COOLDOWN_H", 6))


def _log(msg: str) -> None:
    print(msg, flush=True)


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否存活。查不到时保守返回 True（宁可不跑，也不并发抢配额）。"""
    if pid <= 0:
        return False
    if os.name == "posix":
        # 信号 0 不发送、只做权限与存在性检查：进程不存在 → ProcessLookupError。
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True     # 存在但不属于本用户 → 保守当存活
        except OSError:
            return True
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, timeout=15)
        txt = out.stdout.decode("utf-8", errors="replace")
    except Exception:
        return True
    return str(pid) in txt


def _posix_enrich_pids() -> list[int]:
    """macOS/Linux：pgrep -f 取候选，再用 ps 校验命令行（pgrep 天然不返回自身）。

    必须二次校验：pgrep -f 匹配整条命令行，而本启动器自己的命令行里也可能出现
    目标脚本名（如 --extra-args 被透传）。只保留「含 backfill_enrich_labels.py
    且不含 run_enrich_daily.py」的行，语义与 Windows 版等价。
    """
    try:
        out = subprocess.run(["pgrep", "-f", "backfill_enrich_labels.py"],
                             capture_output=True, timeout=30)
    except Exception as e:                      # 与 Windows 版同：交回锁文件兜底
        _log(f"  ⚠️  进程扫描失败({type(e).__name__})，仅依赖锁文件判断")
        return []
    me, parent = os.getpid(), os.getppid()
    pids: list[int] = []
    for tok in out.stdout.decode("utf-8", errors="replace").split():
        if not tok.isdigit():
            continue
        pid = int(tok)
        if pid in (me, parent):
            continue
        try:
            cmd = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True, timeout=15
            ).stdout.decode("utf-8", errors="replace")
        except Exception:
            cmd = ""
        if "backfill_enrich_labels.py" in cmd and "run_enrich_daily.py" not in cmd:
            pids.append(pid)
    return pids


def find_running_enrich_pids() -> list[int]:
    """找出命令行含 backfill_enrich_labels.py 的存活 python 进程（排除自己）。"""
    if os.name == "posix":
        return _posix_enrich_pids()
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


def _db_target() -> tuple[str, int] | None:
    """解析 DB 的 host:port（优先环境变量，其次 scripts/.env）。解析不出返回 None。

    注意子脚本的 DATABASE_URL 是 config.py 直接读 scripts/.env 得到的，与 cwd 无关；
    本启动器原先不感知 DB，这里为「唤醒瞬间 Wi-Fi 未关联」的预检补上最小取数逻辑。
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        try:
            for line in (SCRIPTS_DIR / ".env").read_text(
                    encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            return None
    m = re.search(r"@([^/?#@]+?):(\d+)(?:/|$)", url)
    return (m.group(1), int(m.group(2))) if m else None


def wait_db_ready(max_wait: int = 120) -> bool:
    """启动子进程前确认 DB 端口可达。解析不出 host:port 时保守放行（交给子脚本报错）。

    笔记本合盖唤醒的瞬间 Wi-Fi 常未关联，不预检就会白跑一整轮。
    """
    target = _db_target()
    if not target:
        return True
    host, port = target
    deadline = time.time() + max_wait
    attempt = 0
    while True:
        attempt += 1
        try:
            with socket.create_connection((host, port), timeout=5):
                if attempt > 1:
                    _log(f"  ✅ DB 已可达（{host}:{port}，第 {attempt} 次尝试）")
                return True
        except OSError as e:
            if time.time() >= deadline:
                _log(f"  ⛔ DB 不可达（{host}:{port}，重试 {attempt} 次"
                     f"共 {max_wait}s）：{e}")
                return False
            time.sleep(5)


def _in_daily_guard(now: datetime) -> bool:
    """热跑是否落在每日全量的前后保护窗口内（用于让位）。"""
    start = now.replace(hour=DAILY_TRIGGER_HOUR, minute=0, second=0, microsecond=0)
    return abs((now - start).total_seconds()) <= HOT_DAILY_GUARD_MIN * 60


def clean_old_logs() -> None:
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    # launchd 的 StandardOutPath/StandardErrorPath 只追加、不轮转，一并按 mtime 清理
    # （这两个文件名不含日期，只能靠 mtime；正在写的文件 mtime 是新的，不会被误删）。
    patterns = ("enrich_*.log", "launchd_enrich_*.out", "launchd_enrich_*.err")
    for pattern in patterns:
        for f in LOG_DIR.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
            except Exception:
                pass


# ── 出口 IP 告警（403 熔断的配套）────────────────────────────────
# 为什么必须发邮件：403 是「线路不对」，不会自愈，等下去只会天天空跑；
# 而看门狗如果只在日志里喊，没人会主动去翻 ~/.workbuddy/logs。

def _ip_alert_file() -> Path:
    return LOG_DIR / "enrich_ip_alert.state"


def _load_ip_state() -> dict:
    try:
        return json.loads(_ip_alert_file().read_text(encoding="utf-8"))
    except Exception:
        return {}          # 首次运行 / 文件损坏都按「无状态」处理


def _save_ip_state(state: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _ip_alert_file().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        _log(f"  ⚠️  IP 告警状态写入失败（不影响本轮）: {e}")


def _egress_ip(timeout: int = 6) -> str:
    """尽力探测当前出口 IP，只用于告警文案。失败返回空串，不影响主流程。"""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _send_mail(subject: str, body_html: str, from_name: str) -> tuple[bool, str]:
    """发给系统管理员（ADMIN_EMAIL，未配置回退 SMTP_TO）。

    crypto_research 的导入刻意放在函数内：告警只是旁路，缺依赖或环境变量时
    最多少发一封信，绝不能让正在跑的富化轮次因为告警模块导入失败而崩掉。
    """
    try:
        if str(SRC_DIR) not in sys.path:
            sys.path.insert(0, str(SRC_DIR))
        from crypto_research.config import get_settings
        from crypto_research.clients.notifier import EmailNotifier
        settings = get_settings(require_database=False)
        notifier = EmailNotifier(settings)
        if not notifier.configured:
            return False, "SMTP 未配置（缺少 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO）"
        return notifier.send(subject, body_html, from_name=from_name,
                             to=settings.admin_email or settings.smtp_to)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def alert_bad_egress_ip(mode_txt: str, bad_streak: int, log_path: Path,
                        cooldown_h: float) -> None:
    """403 熔断后发「出口 IP 被拦」告警（带冷却，避免每 30 分钟一封）。"""
    state = _load_ip_state()
    now = datetime.now()
    last = state.get("bad_alert_at")
    if last:
        try:
            if now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S") < timedelta(hours=cooldown_h):
                _log(f"  ℹ️  IP 告警仍在 {cooldown_h:g} 小时冷却期内"
                     f"（上次 {last}），本次不重复发信")
                return
        except ValueError:
            pass

    ip = _egress_ip() or "探测失败"
    body = f"""
    <div style="font-family:sans-serif;max-width:660px;margin:auto;font-size:14px;line-height:1.75">
      <h2 style="margin:0 0 10px">⚠️ 地址标签富化卡住：出口 IP 被 Cloudflare 拦截</h2>
      <p><b>时间</b>：{now:%Y-%m-%d %H:%M:%S}<br>
         <b>任务</b>：{mode_txt}（本机 launchd，每 30 分钟一轮）<br>
         <b>现象</b>：连续 {bad_streak} 批大面积 403（每批 ≥{BAD_BATCH_403} 个失败），
             本次运行已主动中止<br>
         <b>当前出口 IP</b>：<code>{ip}</code></p>
      <p><b>原因</b>：Etherscan 系列区块浏览器对机房/IDC 出口 IP 有 Cloudflare 拦截。
         403 说明这条线路被判为数据中心或风险 IP，<b>与本机网络快慢无关，只能换节点</b>。</p>
      <p><b>要做的</b>：把代理 / VPN 切到非机房（住宅、家宽）节点，然后自检这两条：</p>
      <pre style="background:#f6f8fa;padding:10px;border-radius:6px;overflow-x:auto">curl -s --noproxy '*' https://api.ipify.org
curl -s -o /dev/null -w '%{{http_code}}\\n' --noproxy '*' \\
  -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36' \\
  https://etherscan.io/address/0x0000000000000000000000000000000000000000
# 期望：第一行不是机房 IP，第二行 200</pre>
      <p><b>不用担心的</b>：403 失败的地址<b>不会</b>写进
         <code>onchain_label_fetch_attempt</code>，积压不会被刷成假的「已尝试」；
         换好节点后 30 分钟内自动续跑，标签恢复正常入库时会再收到一封「已恢复」。</p>
      <p style="color:#888;font-size:12px">本提醒 {cooldown_h:g} 小时内最多一封。<br>
         日志：{log_path}</p>
    </div>"""
    ok, msg = _send_mail(f"【需人工处理】地址标签富化出口 IP 被拦（{ip}），请更换代理节点",
                         body, from_name="地址标签富化看门狗")
    _log(f"  {'📧 已发送' if ok else '❌ 发送失败'}出口 IP 告警: {msg}")
    if ok:
        state["bad_alert_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    # 无论邮件是否发出都记「bad」：网络恢复后据此补发「已恢复」
    state["state"] = "bad"
    _save_ip_state(state)


def alert_egress_ip_recovered(mode_txt: str, log_path: Path) -> None:
    """之前进入过坏 IP 状态、这一轮又正常跑出了批次 → 补发一封「已恢复」。"""
    state = _load_ip_state()
    if state.get("state") != "bad":
        return
    now = datetime.now()
    ip = _egress_ip() or "探测失败"
    body = f"""
    <div style="font-family:sans-serif;max-width:660px;margin:auto;font-size:14px;line-height:1.75">
      <h2 style="margin:0 0 10px">✅ 地址标签富化已恢复</h2>
      <p><b>时间</b>：{now:%Y-%m-%d %H:%M:%S}<br>
         <b>任务</b>：{mode_txt}（本机 launchd）<br>
         <b>当前出口 IP</b>：<code>{ip}</code><br>
         <b>现象</b>：本轮已正常跑出批次，403 不再大面积出现，标签恢复正常入库。</p>
      <p>无需操作，积压会按每 30 分钟一轮继续消化。</p>
      <p style="color:#888;font-size:12px">上次告警：{state.get('bad_alert_at') or '（未发出）'}<br>
         日志：{log_path}</p>
    </div>"""
    ok, msg = _send_mail(f"【已恢复】地址标签富化出口 IP 正常（{ip}）",
                         body, from_name="地址标签富化看门狗")
    _log(f"  {'📧 已发送' if ok else '❌ 发送失败'}恢复通知: {msg}")
    if ok:
        state["state"] = "ok"
        state["recovered_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state.pop("bad_alert_at", None)
        _save_ip_state(state)


def _start_keepawake():
    """macOS：本轮运行期间拦掉「空闲休眠」，返回 caffeinate 进程（可 None）。

    2026-10-01 实测：这台笔记本在电池 + 空闲时几乎每分钟都在睡（pmset 日志里
    12:12 / 12:15 / 13:06… 反复 Entering Sleep）。一轮跑到一半被睡，子进程连同
    网络请求一起被冻住，白耗到运行上限才被杀——曾把它误判成「本机速度只有
    0.1 地址/秒」。

    为什么是 caffeinate -i：-i 只拦「空闲休眠」，合盖/主动睡眠照样生效，符合
    「只在我用电脑时跑」的定位。为什么用 -w 绑自己的 PID 而不是直接包住子进程：
    这样本进程一退（正常结束、超时强杀、被 launchd 收掉）断言立刻释放，不会把
    机器长期吊着不让睡；同时子进程仍是直接子进程，terminate() 语义不变。
    """
    if os.name != "posix" or not Path("/usr/bin/caffeinate").exists():
        return None
    try:
        return subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _log(f"  ⚠️  caffeinate 启动失败（不影响本轮）: {e}")
        return None


def main() -> int:
    global LOG_DIR, LOCK_FILE          # 必须在任何引用之前声明

    parser = argparse.ArgumentParser(
        description="本地地址标签富化调度启动器（单实例 + 实时日志）")
    parser.add_argument("--mode", choices=("daily", "hot", "backlog"), default="daily",
                        help="daily=每日全量（清 medium + 长尾）；"
                             "hot=每 30 分钟热跑（只捞最近 4h 新地址）；"
                             "backlog=每 30 分钟清积压长尾（不限窗口）")
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
    mode_txt = {"hot": "热跑", "backlog": "清积压", "daily": "每日全量"}[mode]
    # 整体运行上限：launchd 无 ExecutionTimeLimit，卡死的子进程会永久占锁
    max_runtime_min = float(os.environ.get(
        "ENRICH_MAX_RUNTIME_MIN", MAX_RUNTIME_MIN.get(mode, 25)))
    cmd = [sys.executable, "-u", str(TARGET_SCRIPT), *MODE_ARGS[mode]]
    if args.extra_args.strip():
        cmd += args.extra_args.strip().split()

    _log("=" * 68)
    _log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 地址标签富化（{mode_txt}）")
    _log(f"  工作目录: {SCRIPTS_DIR}")
    _log(f"  解释器  : {sys.executable}")
    _log(f"  运行上限: {max_runtime_min:.0f} 分钟")
    _log(f"  命令    : {' '.join(cmd)}")

    if args.dry_run:
        _log("  [dry-run] 未实际执行")
        return 0

    # 热跑/清积压避开每日全量的触发时刻：三者共用一把锁，只要 30 分钟档先抢到锁，
    # 当日全量就会被单实例保护挡掉，medium 验证与长尾就整整一天不跑。
    if mode != "daily" and not args.force and _in_daily_guard(datetime.now()):
        _log(f"  ⏸ 距每日全量（{DAILY_TRIGGER_HOUR:02d}:00）不足 "
             f"{HOT_DAILY_GUARD_MIN} 分钟，本次{mode_txt}让位跳过")
        return 0

    # 日志文件必须在任何分支判断之前打开：
    # 计划任务下控制台输出会丢弃，若「跳过」不留痕，事后无法区分
    # 「任务没跑」和「跑了但被单实例保护跳过」——踩过这个坑。
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 三种模式分开记日志：30 分钟档一天 48 次，混进全量日志里会把一夜的长跑日志淹掉
    log_path = LOG_DIR / f"enrich_{mode}_{datetime.now():%Y%m%d}.log"
    t0 = time.time()
    last_label_n = 0
    last_upgrade_n = 0
    exit_code = -1
    # 只有本进程真正取得锁才允许释放：所有「跳过」分支（DB 不可达 / 让位 /
    # 已有实例 / 未取得锁）都会走到下面的 finally，若无条件调 release_lock()，
    # 就会把「另一个正在跑的实例」的锁文件删掉——锁作为进程扫描失败时的兜底
    # 就此失效，可能撞车双跑烧配额（2026-10-01 实测复现）。
    lock_acquired = False

    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            def emit(msg: str) -> None:
                """控制台 + 日志文件双写，任何分支都留痕。"""
                print(msg, flush=True)
                lf.write(msg.rstrip("\n") + "\n")
                lf.flush()

            emit(f"\n{'=' * 60}\n"
                 f"[{datetime.now():%Y-%m-%d %H:%M:%S}] START\n"
                 f"CMD: {' '.join(cmd)}\n"
                 f"上限: {max_runtime_min:.0f} 分钟\n{'=' * 60}")

            # --- 网络就绪预检（放在取锁之前：不通就不占用锁、不写锁）---
            if not wait_db_ready():
                emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] END "
                     f"exit=0 耗时={(time.time() - t0) / 60:.1f}分钟 "
                     f"结果=跳过(DB 不可达)")
                return 0

            # --- 单实例保护 ---
            # 工作电脑场景（2026-09-30）：只有工作日开机，全量改为 09:00 触发 +
            # 开机补跑（StartWhenAvailable）。周一开机时全量与热跑可能同时补跑，
            # 热跑若先抢到锁会把全量整个挤掉 ⇒ 全量改为「等占用者跑完再上」
            # （上限 45 分钟，等不到才放弃）；热跑保持立即让位。
            running = find_running_enrich_pids()
            if running and not args.force:
                if mode == "daily":
                    wait_max = 45 * 60
                    waited = 0
                    emit(f"  ⏳ 全量遇到正在运行的实例 (PID={running})，"
                         f"等待其结束再上（最长 45 分钟）")
                    while waited < wait_max:
                        time.sleep(30)
                        waited += 30
                        running = find_running_enrich_pids()
                        if not running:
                            break
                    if running:
                        emit(f"  ⛔ 等待 {waited // 60} 分钟仍有实例在跑 (PID={running})，"
                             f"本次全量放弃")
                        emit(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] END "
                             f"exit=0 耗时={(time.time() - t0) / 60:.1f}分钟 "
                             f"结果=放弃(等待超时)")
                        return 0
                    emit(f"  ✅ 占用实例已结束（等了 {waited // 60} 分钟），全量继续")
                else:
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
            lock_acquired = True

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"       # 保证子进程输出 UTF-8
            env["PYTHONUNBUFFERED"] = "1"
            # 本机直连 prod，不走代理（代理会拦或降速）
            for p in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                      "https_proxy", "ALL_PROXY", "all_proxy"):
                env.pop(p, None)

            # --- 预步骤：刷新 whale 候选榜（仅每日全量，非致命）---
            # 富化队列带 --whale-priority 时按候选榜 score 排序（大额+交易所往来
            # 密集优先）。榜刷新失败只损失排序质量，队列自动退化为频次序。
            if mode == "daily":
                emit("  ── 预步骤：刷新 whale 候选榜（mine_whale_candidates 30d）──")
                try:
                    mine = subprocess.run(
                        [sys.executable, "-u",
                         str(SCRIPTS_DIR / "bin" / "mine_whale_candidates.py"),
                         "--days", "30", "--top", "0"],
                        cwd=str(SCRIPTS_DIR), env=env,
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                        timeout=900)
                    tail = (mine.stdout or "").strip().splitlines()[-12:]
                    for ln in tail:
                        emit(f"  [mine] {ln}")
                    if mine.returncode != 0:
                        emit(f"  ⚠️  候选榜刷新失败 exit={mine.returncode}，"
                             f"队列退化为频次序: {(mine.stderr or '')[-200:]}")
                except Exception as e:
                    emit(f"  ⚠️  候选榜刷新异常（不阻断）: {e}")
                emit("  ── 预步骤完成 ──")

            # 【不要改回 stdout=PIPE 实时读】2026-09-30 实测踩坑：
            # 在 Windows 计划任务会话下，Popen(stdout=PIPE) + 逐行读的模式
            # 子进程输出会被无限期憋住（14 分钟一个字节都到不了父进程，
            # 进程被杀后缓冲才一次性吐出）——同脚本交互终端下完全实时。
            # 熔断器因此全瞎，且日志文件 14 分钟不增长，极易误判卡死被杀。
            # 现改为：子进程 stdout 直接绑日志文件（绕开 PIPE），
            # 父进程定期 seek 读文件尾部做 429 熔断 + 计数提取。
            # 子进程 stdout 直接绑日志文件（见下方 Popen 注释）
            log_handle = lf

            # 运行期防睡：这一轮跑完之前别让机器空闲休眠（详见 _start_keepawake）
            keepawake = _start_keepawake()
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
            bad403_streak = 0
            halted_by_403 = False
            batches_done = 0          # 本轮真正跑出多少批（用于「已恢复」判定）
            timed_out = False
            while proc.poll() is None:
                time.sleep(30)
                # 整体运行上限兜底（launchd 没有 Windows 计划任务的
                # ExecutionTimeLimit）。子进程若卡死（网络黑洞 / explorer 挂起）
                # 会永久占锁，之后每一轮都被单实例保护跳过 → 页面标签年龄突破
                # onchain_alert.py 的 LABEL_STALE_HOURS=26h。到点强杀，未爬地址
                # 不入 attempt 表，下一轮自动续跑。
                if (time.time() - t0) / 60 >= max_runtime_min:
                    emit(f"  ⏰ 已达运行上限 {max_runtime_min:.0f} 分钟，强制结束"
                         f"本次运行（已爬到标签已入库，下一轮续跑）")
                    proc.terminate()
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    timed_out = True
                    break
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
                    if line.lstrip().startswith("[watch]"):
                        continue   # 防御：历史日志里残留的 watch 行不参与计数/熔断
                    m = LABEL_RE.search(line)
                    if m:
                        last_label_n = int(m.group(1))
                    mu = UPGRADE_RE.search(line)
                    if mu:
                        last_upgrade_n += int(mu.group(1))

                    # 429 熔断（从文件增量里检测，与旧 PIPE 逻辑同阈值）
                    if "批次完成" in line:
                        batches_done += 1
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

                        # 403 熔断：连续多批大面积 403 = 这条线路的出口 IP 被
                        # Cloudflare 判为机房/风险 IP（换节点才会好，等下去不会
                        # 自愈）。与 429 分开计数，两者原因与处置完全不同。
                        m403 = FAIL403_RE.search(line)
                        n403 = int(m403.group(1)) if m403 else 0
                        if n403 >= BAD_BATCH_403:
                            bad403_streak += 1
                        else:
                            bad403_streak = 0
                        if bad403_streak >= BAD_BATCH_STREAK_403:
                            emit(f"  🛑 连续 {bad403_streak} 批大面积 403（每批 "
                                 f"≥{BAD_BATCH_403} 个失败），判定出口 IP 被 "
                                 f"Cloudflare 拦截，主动中止本次运行")
                            emit("     403 不进 attempt 表，积压不会丢；"
                                 "换干净节点后自动续跑（同时发邮件提醒）")
                            proc.terminate()
                            halted_by_403 = True
                            break
                if halted_by_429 or halted_by_403:
                    break
                # 注意：不要把手扫描到的进度行再 emit 回日志文件——子进程本来就
                # 直写同一文件，回写会制造重复行，且含「批次完成」的回写行会被
                # 下一轮扫描再次命中 → 计数翻倍、[watch] 前缀递归嵌套、
                # 429 熔断被同一条旧行重复计数而误触发（2026-09-30 实测）。

            try:
                exit_code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait()

            # 子进程已结束，还机器自由（caffeinate 也绑了本进程 PID，即使这里
            # 出意外，本进程退出时它也会自行释放）
            if keepawake:
                try:
                    keepawake.terminate()
                except Exception:
                    pass

            elapsed = time.time() - t0
            if halted_by_429:
                tail = f"（因限流熔断中止，已处理约 {bad_streak * 50} 个的最后窗口）"
            elif halted_by_403:
                tail = (f"（因出口 IP 被拦【403】熔断中止，已提醒换节点；"
                        f"处理了 {batches_done} 批）")
            elif timed_out:
                tail = f"（达 {max_runtime_min:.0f} 分钟上限截断，下一轮续跑）"
            else:
                tail = ""
            summary = (f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] END "
                       f"exit={exit_code} 耗时={elapsed / 60:.1f}分钟 "
                       f"新增标签≈{last_label_n} "
                       f"medium→high={last_upgrade_n}{tail}\n")
            lf.write(summary)
            _log(summary.strip())

            # 出口 IP 告警（403 熔断的配套）。放在子进程结束、锁释放之前：
            # 发信失败只记日志，绝不影响本轮退出码与锁的归还。
            #   ① 本轮因 403 熔断 → 提醒换节点（6 小时冷却，避免每轮一封）
            #   ② 跑出过批次且没被 403 拦住 → 若此前处于「坏 IP」状态则补发
            #      「已恢复」（batches_done=0 的空轮不发，避免候选池为空时误报恢复）
            try:
                if halted_by_403:
                    alert_bad_egress_ip(mode_txt, bad403_streak, log_path,
                                        IP_ALERT_COOLDOWN_H)
                elif batches_done > 0:
                    alert_egress_ip_recovered(mode_txt, log_path)
            except Exception as e:
                _log(f"  ⚠️  出口 IP 告警异常（不影响本轮）: {type(e).__name__}: {e}")

            # 超时/熔断是「按计划主动停止」，不是运行失败：子进程被 SIGTERM 收掉，
            # proc.wait() 得到 -15，若原样当退出码返回，launchd 会把每一轮都记成
            # last exit status = 241（256-15），`launchctl list` 看起来像天天失败。
            # 对外统一归零；真实信号仍留在上面的 SUMMARY 里可查。
            if timed_out or halted_by_429 or halted_by_403:
                exit_code = 0
    finally:
        if lock_acquired:
            release_lock()
        clean_old_logs()

    _log(f"  日志: {log_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
