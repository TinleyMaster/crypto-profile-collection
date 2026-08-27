"""
Phase 1: 大额转账监控（自动循环）。
存储双向大额转账（保证 CEX netflow 计算完整），告警关注转入交易所的潜在砸盘信号。

P1 修复（2026-08-27）：改为按链独立循环，每链从 offset=0 递增直至该链覆盖完毕，
避免高市值链（eth/bsc/solana）霸占全局排序前段、长尾链永远扫不到的问题。
"""

from __future__ import annotations

import os
import re
import sys
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

sys.stdout.reconfigure(line_buffering=True)

BATCH_SIZE = 30
MAX_ROUNDS_PER_CHAIN = 1000   # 单链安全上限（正常应在覆盖完毕前自然停止）
TIMEOUT = 1800
# 与 phase_chain_transfer_monitor.py SUPPORTED_CHAINS 保持一致
SUPPORTED_CHAINS = ("eth", "bsc", "solana", "polygon", "arbitrum", "base",
                    "optimism", "avalanche", "tron", "ton", "sui", "aptos")


def run_chain_loop(chain: str) -> tuple[int, int]:
    """单链分批扫描，返回 (处理条数, 告警条数)。"""
    script = os.path.join(SCRIPT_DIR, "phase_chain_transfer_monitor.py")
    total_processed = 0
    total_alerts = 0
    offset = 0
    zero_consecutive = 0

    for round_num in range(1, MAX_ROUNDS_PER_CHAIN + 1):
        print(f"\n{'='*60}")
        print(f"[{chain}] Round {round_num} / max {MAX_ROUNDS_PER_CHAIN}  | batch={BATCH_SIZE} offset={offset}")
        print(f"{'='*60}")

        try:
            result = subprocess.run(
                [sys.executable, "-u", script,
                 "--chain", chain,
                 "--limit", str(BATCH_SIZE), "--offset", str(offset), "--alarm-only"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                cwd=SCRIPT_DIR,
            )
        except subprocess.TimeoutExpired:
            print(f"  [{chain}] Round {round_num} 超时（>{TIMEOUT}s），终止该链")
            break

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if output:
            for line in output.splitlines():
                print(f"  {line}")

        if stderr:
            print(f"  [stderr] {stderr[:500]}")

        if result.returncode != 0:
            print(f"  [{chain}] Round {round_num} 异常退出 (code={result.returncode})，终止该链")
            break

        # 本轮实际监控的资产数（monitor 输出 "共 N 个资产待监控"）。
        round_assets = -1
        for line in output.splitlines():
            if "个资产待监控" in line:
                m = re.search(r"共\s*(\d+)\s*个资产待监控", line)
                if m:
                    round_assets = int(m.group(1))
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

        print(f"  [{chain}] 累计: 处理={total_processed}  告警={total_alerts}  (本批资产={max(round_assets, 0)})")

        # 该链本轮无可监控资产 => offset 已越过全部资产，该链覆盖完毕
        if round_assets == 0:
            print(f"  [{chain}] 已覆盖全部资产，停止该链")
            break

        if round_processed == 0:
            zero_consecutive += 1
            # 安全阀：连续多批无新数据才停止，避免该链长尾无数据时提前终止。
            # offset 越界（round_assets=0）时会优先停止，不会真的跑满。
            if zero_consecutive >= 50:
                print(f"  [{chain}] 连续多批无新数据，停止（安全阀）")
                break
        else:
            zero_consecutive = 0

        offset += BATCH_SIZE
        time.sleep(2)

    return total_processed, total_alerts


def main():
    total_processed = 0
    total_alerts = 0

    for chain in SUPPORTED_CHAINS:
        print(f"\n{'#'*60}")
        print(f"# 开始扫描链: {chain}")
        print(f"{'#'*60}")
        try:
            processed, alerts = run_chain_loop(chain)
            total_processed += processed
            total_alerts += alerts
        except Exception as e:  # noqa: BLE001
            print(f"  [{chain}] 循环异常: {e}")
            continue
        time.sleep(2)

    print(f"\nAll chains complete.  累计: 处理={total_processed}  告警={total_alerts}")


if __name__ == "__main__":
    main()