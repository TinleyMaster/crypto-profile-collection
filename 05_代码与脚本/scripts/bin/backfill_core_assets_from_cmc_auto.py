"""
CMC 资产全量入库自动循环：持续运行直到所有 CMC 资产都写入 core.asset。
"""
import subprocess
import sys
import os
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_SIZE = 500        # 每批资产数
MAX_ROUNDS = 200        # 安全上限（500 × 200 = 100,000，覆盖全部 CMC 资产）

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_created = 0
total_refreshed = 0

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'='*60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_SIZE}  新建={total_created}  刷新={total_refreshed}")
    print(f"{'='*60}")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "refresh_core_assets_from_cmc.py"),
            "--limit",
            str(BATCH_SIZE),
        ],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"[ERROR] 脚本退出码 {result.returncode}")
        if result.stderr:
            print(result.stderr[:500])
        break

    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        print(f"[ERROR] 无法解析输出: {result.stdout.strip()[:300]}")
        break

    processed = data.get("processed_rows", 0)
    created = data.get("created_assets", 0)
    refreshed = data.get("refreshed_assets", 0)

    if processed == 0:
        print("无更多候选资产，全部完成。")
        break

    total_created += created
    total_refreshed += refreshed
    print(f"本轮: {processed} 条处理, {created} 新建, {refreshed} 刷新")

print(f"\n全部完成。累计: {total_created} 新建, {total_refreshed} 刷新")
