"""
DexScreener 补充文档入口自动循环脚本。

持续运行 supplement_doc_entries_from_dexscreener 直到候选资产全部处理完。
"""

import subprocess
import sys
import os
from pathlib import Path

# 行缓冲：确保 print 实时输出（stdout 是 pipe 时默认全缓冲）
sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 50  # 每批 50 个资产
MAX_ROUNDS = 200  # 安全上限

# 设置环境
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
                str(SCRIPT_DIR / "supplement_doc_entries_from_dexscreener.py"),
                "--limit",
                str(BATCH_LIMIT),
            ],
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,  # 单轮最长 10 分钟
        )
    except subprocess.TimeoutExpired:
        print("本轮超时（10分钟），跳过继续。")
        continue

    # 打印完整输出
    for line in result.stdout.strip().splitlines():
        print(line)

    if result.returncode != 0:
        print(f"Script exited with code {result.returncode}, stopping.")
        if result.stderr:
            print(f"stderr: {result.stderr[:500]}")
        break

    # 解析结果
    import json
    summary = None
    for line in result.stdout.strip().splitlines():
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

        total_assets += matched
        total_entries += entries

        if candidates == 0:
            print("\n无更多候选资产，全部完成！")
            break

        if matched == 0:
            print("\n本轮无匹配，可能剩余资产在 DexScreener 中无数据，停止。")
            break
    else:
        print("无法解析本轮结果，继续下一轮。")

print(f"\nAll rounds complete.  累计: {total_assets} 资产, {total_entries} 入口")