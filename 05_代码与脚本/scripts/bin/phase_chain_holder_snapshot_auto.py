"""
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
    script = os.path.join(SCRIPT_DIR, "phase_chain_holder_snapshot.py")

    print("=" * 60)
    print("链上持仓快照采集（每日单次）")
    print("=" * 60)

    t0 = time.time()

    proc = subprocess.Popen(
        [sys.executable, "-u", script, "--limit", "0"],  # 0 = 不限量，全量处理
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