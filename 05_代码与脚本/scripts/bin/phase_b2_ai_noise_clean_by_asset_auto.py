"""B4 AI 噪声清理（按资产分组）— 自动循环。"""
from __future__ import annotations

import subprocess
import sys
import re
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SCRIPT = SCRIPT_DIR / "phase_b2_ai_noise_clean_by_asset.py"

MAX_ROUNDS = 100
ASSETS_PER_ROUND = 20
# 单轮超时（每个资产约 30s LLM + 抓取，20 个资产留 20 分钟余量）
ROUND_TIMEOUT_SECONDS = 20 * 60
# 总时长上限（避免跑一整天占着调度槽位，默认 4 小时）
TOTAL_MAX_SECONDS = 4 * 3600


def main():
    start_time = time.time()

    for rnd in range(1, MAX_ROUNDS + 1):
        # 总时长检查
        elapsed = time.time() - start_time
        if elapsed >= TOTAL_MAX_SECONDS:
            print(f"\n已运行 {elapsed/3600:.1f}h，达到总时长上限，停止。")
            break

        print(f"\n{'=' * 60}")
        print(f"  Round {rnd} / max {MAX_ROUNDS}  |  每轮 {ASSETS_PER_ROUND} 个资产"
              f"  |  已运行 {elapsed/60:.0f}min")
        print(f"{'=' * 60}")

        try:
            cp = subprocess.run(
                [PYTHON, "-u", str(SCRIPT), "--execute", "--limit", str(ASSETS_PER_ROUND)],
                cwd=str(SCRIPT_DIR.parent),
                capture_output=True, text=True,
                timeout=ROUND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(f"  ⚠️  本轮超时（>{ROUND_TIMEOUT_SECONDS}s），跳过继续下一轮")
            continue

        print(cp.stdout)
        if cp.stderr:
            print(cp.stderr, file=sys.stderr)

        # 检查是否还有剩余资产
        # 从输出中提取：处理资产: X 个
        m = re.search(r"处理资产:\s*(\d+)\s*个", cp.stdout)
        if m and int(m.group(1)) == 0:
            print("全部完成。")
            break

        if cp.returncode != 0:
            print(f"本轮出错，退出 (rc={cp.returncode})")
            break

    print("自动循环结束。")


if __name__ == "__main__":
    main()