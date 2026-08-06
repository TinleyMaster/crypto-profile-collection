"""
CMC 补充文档入口自动循环：持续运行直到无更多候选资产。
"""

import subprocess
import sys
import os
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 200       # 每批资产数
MAX_ROUNDS = 100        # 安全上限

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_assets = 0
total_entries = 0

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计资产={total_assets}  累计入口={total_entries}")
    print(f"{'=' * 60}")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "refresh_doc_source_entries_from_cmc.py"),
            "--limit",
            str(BATCH_LIMIT),
        ],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"脚本退出码 {result.returncode}，停止。")
        print(result.stderr)
        break

    try:
        data = json.loads(result.stdout.strip())
        asset_count = data.get("asset_count", 0)
        entry_count = data.get("entry_count", 0)

        if asset_count == 0:
            print("无更多候选资产，全部完成。")
            break

        total_assets += asset_count
        total_entries += entry_count
        print(f"本轮: {asset_count} 资产, {entry_count} 条入口")
    except (json.JSONDecodeError, ValueError):
        print(f"无法解析输出: {result.stdout.strip()[:200]}")
        break

print(f"\n全部完成。累计: {total_assets} 资产, {total_entries} 条入口")
