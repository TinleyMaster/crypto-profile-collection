"""
Phase 1: 链上持仓快照采集（自动循环）。
每轮处理一批资产，直到全部完成。
"""

from __future__ import annotations

import os
import sys
import subprocess
import time

# 确保能找到 src 模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

sys.stdout.reconfigure(line_buffering=True)

BATCH_SIZE = 20
MAX_ROUNDS = 500
TIMEOUT = 1800  # 30 分钟


def main():
    script = os.path.join(SCRIPT_DIR, "phase_chain_holder_snapshot.py")

    total_success = 0
    total_skip = 0

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

        # 统计本轮结果
        success_count = 0
        skip_count = 0
        for line in output.splitlines():
            if "成功" in line and "跳过" in line:
                # 格式: "完成: X 成功, Y 跳过, 耗时 Zs"
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if "成功" in p:
                        try:
                            success_count = int(p.split()[0])
                        except ValueError:
                            pass
                    elif "跳过" in p:
                        try:
                            skip_count = int(p.split()[0])
                        except ValueError:
                            pass
                break

        total_success += success_count
        total_skip += skip_count

        print(f"  累计: 成功={total_success}  跳过={total_skip}")

        if success_count == 0 and skip_count == 0:
            print("  本轮无数据，可能已全部处理完成")
            break

        time.sleep(2)

    print(f"\nAll rounds complete.  累计: 成功={total_success}  跳过={total_skip}")


if __name__ == "__main__":
    main()