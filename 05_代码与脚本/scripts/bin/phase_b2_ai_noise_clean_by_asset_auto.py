"""B4 AI 噪声清理（按资产分组）— 自动循环。"""
from __future__ import annotations

import subprocess
import sys
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SCRIPT = SCRIPT_DIR / "phase_b2_ai_noise_clean_by_asset.py"

MAX_ROUNDS = 100
ASSETS_PER_ROUND = 20


def main():
    for rnd in range(1, MAX_ROUNDS + 1):
        print(f"\n{'=' * 60}")
        print(f"  Round {rnd} / max {MAX_ROUNDS}  |  每轮 {ASSETS_PER_ROUND} 个资产")
        print(f"{'=' * 60}")

        cp = subprocess.run(
            [PYTHON, "-u", str(SCRIPT), "--execute", "--limit", str(ASSETS_PER_ROUND)],
            cwd=str(SCRIPT_DIR.parent),
            capture_output=True, text=True,
        )
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