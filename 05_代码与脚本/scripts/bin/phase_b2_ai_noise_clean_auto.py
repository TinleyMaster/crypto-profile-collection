"""
B4 AI 噪声清理自动循环：持续运行直到可疑条目全部处理完或达到上限。
"""

import subprocess
import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 2000       # 每批条数（AI 批内速度够快，可以加大）
MAX_ROUNDS = 100         # 安全上限
TOTAL_LIMIT = 100000     # 总处理上限
BATCH_SIZE = 100         # 每次 AI 调用的条目数
RPM = 300                # AI 调用速率限制（次/分钟）

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_done = 0
total_noise = 0

for round_num in range(1, MAX_ROUNDS + 1):
    remaining = TOTAL_LIMIT - total_done
    if remaining <= 0:
        print(f"\n已达总上限 {TOTAL_LIMIT} 条，停止。")
        break

    batch_size = min(BATCH_LIMIT, remaining)

    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={batch_size}  累计={total_done}/{TOTAL_LIMIT}")
    print(f"{'=' * 60}")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "phase_b2_ai_noise_clean.py"),
            "--limit",
            str(batch_size),
            "--batch-size",
            str(BATCH_SIZE),
            "--rpm",
            str(RPM),
            "--execute",
            "--source",
            "remaining",
        ],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=False,
    )

    if result.returncode != 0:
        print(f"脚本退出码 {result.returncode}，停止。")
        break

    total_done += batch_size

print(f"\n全部完成。累计处理: {total_done}")
