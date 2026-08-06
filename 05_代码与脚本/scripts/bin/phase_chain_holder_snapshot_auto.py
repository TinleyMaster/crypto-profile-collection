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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

sys.stdout.reconfigure(line_buffering=True)

TIMEOUT = 1800  # 30 分钟


def main():
    script = os.path.join(SCRIPT_DIR, "phase_chain_holder_snapshot.py")

    print("=" * 60)
    print("链上持仓快照采集（每日单次）")
    print("=" * 60)

    t0 = time.time()

    try:
        result = subprocess.run(
            [sys.executable, "-u", script, "--limit", "0"],  # 0 = 不限量，全量处理
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=SCRIPT_DIR,
        )
    except subprocess.TimeoutExpired:
        print(f"超时（>{TIMEOUT}s），终止")
        return

    output = result.stdout.strip()
    stderr = result.stderr.strip()

    if output:
        for line in output.splitlines():
            print(f"  {line}")

    if stderr:
        print(f"  [stderr] {stderr[:500]}")

    elapsed = time.time() - t0
    print(f"\n每日快照完成，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()