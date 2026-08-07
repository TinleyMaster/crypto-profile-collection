"""
SPA 无头浏览器爬取自动循环脚本。
持续处理 needs_browser=TRUE 的条目直到全部完成。
"""

import subprocess
import sys
import os
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 20
MAX_ROUNDS = 100
BROWSER_CONCURRENCY = 4

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_entries = 0
total_discovered = 0

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计发现={total_discovered} 链接")
    print(f"{'=' * 60}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(SCRIPT_DIR / "phase_b2_spa_browser_crawl.py"),
                "--limit",
                str(BATCH_LIMIT),
                "--concurrency",
                str(BROWSER_CONCURRENCY),
            ],
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=900,  # 15 分钟超时
        )
    except subprocess.TimeoutExpired:
        print("本轮超时（15分钟），跳过继续。")
        continue

    for line in result.stdout.strip().splitlines():
        print(line)

    if result.returncode != 0:
        print(f"Script exited with code {result.returncode}, stopping.")
        if result.stderr:
            print(f"stderr: {result.stderr[:500]}")
        break

    import json
    summary = None
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            summary = json.loads(line)
            if "status" in summary:
                break
        except json.JSONDecodeError:
            continue

    if summary:
        candidates = summary.get("candidates", 0)
        discovered = summary.get("discovered", 0)
        total_discovered += discovered

        if candidates == 0:
            print("\n无更多 SPA 页面待处理，全部完成！")
            break

        print(f"  本轮: candidates={candidates} discovered={discovered}")
    else:
        print("无法解析本轮结果，继续下一轮。")

print(f"\nAll rounds complete.  累计发现: {total_discovered} 链接")