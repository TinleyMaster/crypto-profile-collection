"""
B2 自动循环脚本：持续运行 phase_b2_deep_doc_discovery 直到 docs 类型 pending 降到阈值以下。
"""

import subprocess
import sys
import os
from pathlib import Path

# 行缓冲：确保 print 实时输出（stdout 是 pipe 时默认全缓冲）
sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 1000
MAX_ROUNDS = 200  # 安全上限
THRESHOLD = 500  # 全部可爬类型 pending 低于此数时停止

# 设置环境
env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}")
    print(f"{'=' * 60}")

    # 运行 B2
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "phase_b2_deep_doc_discovery.py"),
                "--limit",
                str(BATCH_LIMIT),
                "--workers",
                "8",
                "--timeout",
                "8",
            ],
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=False,
            timeout=1800,  # 单轮最长 30 分钟，防止卡死
        )
    except subprocess.TimeoutExpired:
        print("B2 脚本本轮超时（30分钟），跳过继续下一轮。")
        continue

    if result.returncode != 0:
        print(f"Script exited with code {result.returncode}, stopping.")
        break

    print("B2 本轮完成，查询 pending 数量...")

    # 检查 pending
    try:
        probe = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "_probe_b2_pending.py")],
            cwd=str(SCRIPT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,  # 探针最长 30 秒
        )
    except subprocess.TimeoutExpired:
        print("探针查询超时，跳过本轮继续。")
        continue

    if probe.returncode != 0:
        print(f"探针脚本异常退出 (code={probe.returncode})，跳过本轮继续。")
        if probe.stderr:
            print(f"stderr: {probe.stderr[:500]}")
        continue

    # 解析 docs pending count
    docs_pending = None
    for line in probe.stdout.splitlines():
        if line.startswith("docs:"):
            docs_pending = int(line.split(":")[1].strip())

    print(probe.stdout.strip())

    if docs_pending is not None and docs_pending <= THRESHOLD:
        print(f"\ndocs pending ({docs_pending}) <= threshold ({THRESHOLD}), done!")
        break

    if docs_pending is not None:
        print(f"docs pending ({docs_pending}) > threshold ({THRESHOLD})，继续下一轮...")

print("\nAll rounds complete.")
