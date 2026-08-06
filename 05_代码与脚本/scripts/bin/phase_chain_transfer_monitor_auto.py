"""
Phase 1: 大额转账监控（告警模式·自动循环）。
只关注转入交易所的大额转账（潜在砸盘信号），不存储所有转账明细。
每轮扫描增量，标记告警。
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

BATCH_SIZE = 30
MAX_ROUNDS = 200
TIMEOUT = 1800


def main():
    script = os.path.join(SCRIPT_DIR, "phase_chain_transfer_monitor.py")

    total_processed = 0
    total_alerts = 0
    zero_consecutive = 0

    for round_num in range(1, MAX_ROUNDS + 1):
        print(f"\n{'='*60}")
        print(f"Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_SIZE}")
        print(f"{'='*60}")

        try:
            result = subprocess.run(
                [sys.executable, "-u", script, "--limit", str(BATCH_SIZE), "--alarm-only"],
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

        round_processed = 0
        round_alerts = 0
        for line in output.splitlines():
            if "处理" in line and "告警" in line:
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if "处理" in p:
                        try:
                            round_processed = int(p.split()[1])
                        except (ValueError, IndexError):
                            pass
                    elif "告警" in p:
                        try:
                            round_alerts = int(p.split()[1])
                        except (ValueError, IndexError):
                            pass
                break

        total_processed += round_processed
        total_alerts += round_alerts

        print(f"  累计: 处理={total_processed}  告警={total_alerts}")

        if round_processed == 0:
            zero_consecutive += 1
            if zero_consecutive >= 3:
                print("  连续3轮无新数据，停止")
                break
        else:
            zero_consecutive = 0

        time.sleep(2)

    print(f"\nAll rounds complete.  累计: 处理={total_processed}  告警={total_alerts}")


if __name__ == "__main__":
    main()