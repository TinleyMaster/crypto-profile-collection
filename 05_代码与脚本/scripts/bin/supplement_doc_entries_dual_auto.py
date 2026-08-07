"""
DexScreener + Binance 双源补充文档入口自动循环脚本。

持续运行 supplement_doc_entries_dual 直到候选资产全部处理完。
"""

import subprocess
import sys
import os
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 50
MAX_ROUNDS = 200

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_assets = 0
total_entries = 0

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计资产={total_assets}  累计入口={total_entries}")
    print(f"{'=' * 60}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(SCRIPT_DIR / "supplement_doc_entries_dual.py"),
                "--limit",
                str(BATCH_LIMIT),
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
        matched = summary.get("matched", 0)
        entries = summary.get("entries", 0)
        candidates = summary.get("candidates", 0)
        ds = summary.get("ds_matched", 0)
        bn = summary.get("bn_matched", 0)

        total_assets += matched
        total_entries += entries

        print(f"  本轮: matched={matched} entries={entries} (DS={ds} BN={bn})")

        if candidates == 0:
            print("\n无更多候选资产，全部完成！")
            break
    else:
        print("无法解析本轮结果，继续下一轮。")

print(f"\nAll rounds complete.  累计: {total_assets} 资产, {total_entries} 入口")