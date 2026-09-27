"""
DEPRECATED（OBI-OPT-SNAPSHOT-FRESHNESS X3，2026-09-27）：
  本文件的**自动化调度角色**已废弃 —— 链上快照现由 scheduler.py 的四条
  `chain_holder_snapshot_*`（分链、每日、错峰 14:00/14:20/14:40/15:00）承担；
  本文件既不被 supervisord.conf 也不被 scheduler.py 调用，已不参与定时采集。

  保留原因：app.py 的 `chain_holder_snapshot_auto`（工作台「链上持仓快照采集（每日单次）」
  手动触发入口）仍指向本文件，直接删除会使该 UI 任务触发即失败。若确定不再需要该手动入口，
  须先删除 app.py 中的 `chain_holder_snapshot_auto` 条目，再删本文件。

  ⚠️ 本文件内部调用 batch 版时用 `--limit 0`（不限量、一次跑完），属长任务，可能长时间
  占用 chain 并发槽位（scheduler.py 史实见「避免每天 5 小时级任务饿死其他 chain 任务」）。
  手动触发前请知悉此影响。

--- 以下为原始说明 ---
Phase 1: 链上持仓快照采集（每日单次模式）。
每天运行一次，拉取全部有合约地址的资产的 Top 持有者数据。
不做循环，一次跑完。
"""

from __future__ import annotations

import os
import sys
import subprocess
import time
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

sys.stdout.reconfigure(line_buffering=True)

TIMEOUT = 1800  # 30 分钟


def _stream_reader(pipe, prefix: str):
    """实时读取子进程输出流。"""
    try:
        for line in iter(pipe.readline, ""):
            if line:
                print(f"{prefix}{line.rstrip()}")
    except (ValueError, OSError):
        pass


def main():
    # 历史（已闭合）：原 phase_chain_holder_snapshot.py 曾被拆分为 batch(调度) + scrape(单币)，
    # 但调度仍指向已不存在的旧文件名，导致每日调度「启动即失败」（P1-3 根因之一）。该问题已由
    # scheduler.py 的分链调度 chain_holder_snapshot_batch 修复；本文件仅为手动入口，内部改调
    # batch 版（全量入口，--limit 0）。详见文件头 DEPRECATED 说明。
    script = os.path.join(SCRIPT_DIR, "phase_chain_holder_batch.py")

    print("=" * 60)
    print("链上持仓快照采集（每日单次）")
    print("=" * 60)

    t0 = time.time()

    proc = subprocess.Popen(
        [sys.executable, "-u", script, "--all-chains", "--limit", "0"],  # 0 = 不限量，全量处理
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=SCRIPT_DIR,
    )

    # 启动实时读取线程
    stdout_thread = threading.Thread(target=_stream_reader, args=(proc.stdout, ""), daemon=True)
    stderr_thread = threading.Thread(target=_stream_reader, args=(proc.stderr, "  [stderr] "), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"超时（>{TIMEOUT}s），终止")
        return

    # 等待读取线程结束
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    elapsed = time.time() - t0
    print(f"\n每日快照完成，耗时 {elapsed:.1f}s, exit_code={proc.returncode}")


if __name__ == "__main__":
    main()