#!/usr/bin/env python3
"""催化剂全链路合并任务：摄入 → AI 预处理 → thesis 重生。

替代原 4 个独立工作台子任务（catalyst_ingest_all / catalyst_ai_process /
catalyst_thesis_regen / catalyst_thesis_regen_cursor）。单次运行完成整条管道。

容错策略：各阶段独立 subprocess 调用，任一阶段非 0 退出不中断后续阶段
（与 ingest 自身"部分源失败仍成功"的哲学一致）；最终退出码取各阶段最高非 0 值。

阶段超时（2026-09-22 修复）：原实现 `subprocess.run(cmd)` **无超时**，任一阶段
因上游挂起（实测：DeepSeek 402 欠费后 AI 阶段/ thesis 阶段长时间无日志）会把整条
管道拖到 task_manager 的 12h 硬超时，期间占满并发槽位、触发看护反复补跑形成恶性
循环。现每阶段加 `CATALYST_STAGE_TIMEOUT_SEC`（默认 5400s=90min）超时，超时即杀掉
该阶段**整个进程组**并继续后续阶段；6 阶段最坏 9h < 12h 硬超时。
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent

# 单阶段超时（秒）：成功整轮实测 25~77min，取 90min 留余量；可用环境变量覆盖。
STAGE_TIMEOUT_SEC = int(os.getenv("CATALYST_STAGE_TIMEOUT_SEC", "5400"))
TIMEOUT_RC = 124  # 与 GNU timeout(1) 一致

STAGES = [
    ("全源增量摄入", "catalyst_ingest_all.py", []),
    ("AI 预处理", "process_catalyst_ai.py", ["--batch-size", "200"]),
    ("impact 因子化", "build_catalyst_impact.py", ["--incremental"]),
    ("thesis 重生(游标模式)", "catalyst_thesis_regen.py", ["--max-assets", "100"]),
    ("决策管道快通道", "phase_catalyst_pipeline.py", ["--fast"]),
    ("决策管道慢通道", "phase_catalyst_pipeline.py", ["--slow"]),
]


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉阶段进程及其整个进程组（与 task_manager._kill_proc_tree 同口径）。"""
    try:
        if os.name == "posix":
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_stage(cmd: list[str], timeout: int) -> int:
    """运行一个阶段；超时则杀掉整个进程组，返回 TIMEOUT_RC。"""
    # start_new_session=True：让阶段自成一个进程组，便于超时后整组清理
    proc = subprocess.Popen(cmd, start_new_session=(os.name == "posix"))
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
        return TIMEOUT_RC


def main() -> int:
    print("=" * 60, flush=True)
    print(f"催化剂全链路（摄入 → AI → thesis），单阶段超时 {STAGE_TIMEOUT_SEC}s", flush=True)
    print("=" * 60, flush=True)

    worst_rc = 0
    for label, script, extra in STAGES:
        cmd = [sys.executable, "-u", str(BIN_DIR / script)] + extra
        print(f"\n>>> [{label}] {script} {' '.join(extra)}", flush=True)
        rc = _run_stage(cmd, STAGE_TIMEOUT_SEC)
        if rc == TIMEOUT_RC:
            print(f"<<< [{label}] 超时（>{STAGE_TIMEOUT_SEC}s）已终止，继续后续阶段", flush=True)
        else:
            print(f"<<< [{label}] 退出码={rc}", flush=True)
        if rc != 0:
            worst_rc = max(worst_rc, rc)
            print(f"⚠️  阶段 [{label}] 非 0 退出，继续执行后续阶段", flush=True)

    print("\n" + "=" * 60, flush=True)
    print(f"催化剂全链路结束，整体退出码={worst_rc}", flush=True)
    print("=" * 60, flush=True)
    return worst_rc


if __name__ == "__main__":
    sys.exit(main())
