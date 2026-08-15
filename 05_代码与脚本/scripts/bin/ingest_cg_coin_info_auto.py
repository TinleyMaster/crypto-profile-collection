"""
CG coin_info 自动循环：持续运行 ingest_cg_coin_info 直到全部拉完或配额用完。
"""

import subprocess
import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 200        # 每批条数
MAX_ROUNDS = 200         # 安全上限（防止无限循环）
MAX_CALLS = 500          # 单次流水线详情调用上限：增量只补 missing，避免一次吃掉大部分月额度
CALLS_PER_MINUTE = 60    # 速率限制（免费 API 更稳健）

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_done = 0

for round_num in range(1, MAX_ROUNDS + 1):
    remaining = MAX_CALLS - total_done
    if remaining <= 0:
        print(f"\n已用完 {MAX_CALLS} 次调用配额，停止。")
        break

    batch_size = min(BATCH_LIMIT, remaining)

    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={batch_size}  累计={total_done}/{MAX_CALLS}")
    print(f"{'=' * 60}")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "ingest_cg_coin_info.py"),
            "--from-list-missing",
            "--limit",
            str(batch_size),
            "--max-calls",
            str(batch_size),
            "--calls-per-minute",
            str(CALLS_PER_MINUTE),
        ],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=False,
    )

    if result.returncode != 0:
        print(f"脚本退出码 {result.returncode}，停止。")
        break

    total_done += batch_size

print(f"\n全部完成。累计调用: {total_done}")
