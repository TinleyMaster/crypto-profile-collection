"""
SPA 回溯扫描自动循环脚本。
持续扫描 B2 已爬取但未标记 needs_browser 的页面，识别 SPA 并标记。
"""

import subprocess
import sys
import os
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 500
WORKERS = 10
MAX_ROUNDS = 100

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_spa = 0

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计 SPA={total_spa}")
    print(f"{'=' * 60}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(SCRIPT_DIR / "phase_b2_retro_scan_spa.py"),
                "--limit",
                str(BATCH_LIMIT),
                "--workers",
                str(WORKERS),
            ],
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("本轮超时（10分钟），跳过继续。")
        continue

    for line in result.stdout.strip().splitlines():
        print(line)

    if result.returncode != 0:
        print(f"Script exited with code {result.returncode}, stopping.")
        if result.stderr:
            print(f"stderr: {result.stderr[:500]}")
        break

    # 从输出中提取 SPA 数量
    spa_count = 0
    output = result.stdout
    for line in output.splitlines():
        if "扫描完成" in line:
            import re
            m = re.search(r"SPA=(\d+)", line)
            if m:
                spa_count = int(m.group(1))
            break
        if "无候选" in line:
            print("\n无更多候选，全部完成！")
            break

    total_spa += spa_count

    if "无候选" in output:
        break

    print(f"  本轮 SPA: {spa_count}")

print(f"\nAll rounds complete.  累计发现 SPA: {total_spa}")