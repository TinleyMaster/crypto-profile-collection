"""
Phase 1: 大额转账监控（自动循环）。
每轮处理一批资产，直到全部完成。
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

BATCH_SIZE = 20
MAX_ROUNDS = 500
TIMEOUT = 1800


def main():
    script = os.path.join(SCRIPT_DIR, "phase_chain_transfer_monitor.py")

    total_processed = 0
    total_written = 0

    for round_num in range(1, MAX_ROUNDS + 1):
        print(f"\n{'='*60}")
        print(f"Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_SIZE}")
        print(f"{'='*60}")

        try:
            result = subprocess.run(
                [sys.executable, "-u", script, "--limit", str(BATCH_SIZE)],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                cwd=SCRIPT_DIR,
            )
        except subprocess.TimeoutExpired:
            print(f"  Round {round_num} 超时（>{TIMEOUT}s），终止")
            break

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if output:
            for line in output.splitlines():
                print(f"  {line}")

        if stderr:
            print(f"  [stderr] {stderr[:500]}")

        if result.returncode != 0:
            print(f"  Round {round_num} 异常退出 (code={result.returncode})，终止")
            break

        # 解析本轮统计
        round_processed = 0
        round_written = 0
        for line in output.splitlines():
            if "处理" in line and "写入" in line:
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if "处理" in p:
                        try:
                            round_processed = int(p.split()[1])
                        except (ValueError, IndexError):
                            pass
                    elif "写入" in p:
                        try:
                            round_written = int(p.split()[1])
                        except (ValueError, IndexError):
                            pass
                break

        total_processed += round_processed
        total_written += round_written

        print(f"  累计: 处理={total_processed}  写入={total_written}")

        if round_processed == 0:
            print("  本轮无新转账，可能已全部处理完成")
            break

        time.sleep(2)

    print(f"\nAll rounds complete.  累计: 处理={total_processed}  写入={total_written}")


if __name__ == "__main__":
    main()