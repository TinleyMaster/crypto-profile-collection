"""
B2 自动循环脚本：持续运行 phase_b2_deep_doc_discovery 直到 docs 类型 pending 降到阈值以下。
"""

import subprocess
import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 500
MAX_ROUNDS = 200  # 安全上限
THRESHOLD = 500  # docs 类型低于此数时停止

# 设置环境
env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}")
    print(f"{'=' * 60}")

    # 运行 B2
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "phase_b2_deep_doc_discovery.py"),
            "--limit",
            str(BATCH_LIMIT),
            "--workers",
            "15",
            "--timeout",
            "8",
        ],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=False,
    )

    if result.returncode != 0:
        print(f"Script exited with code {result.returncode}, stopping.")
        break

    # 检查 pending
    probe = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "_probe_b2_pending.py")],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
    )

    # 解析 docs pending count
    docs_pending = None
    for line in probe.stdout.splitlines():
        if line.startswith("docs:"):
            docs_pending = int(line.split(":")[1].strip())

    print(probe.stdout.strip())

    if docs_pending is not None and docs_pending <= THRESHOLD:
        print(f"\ndocs pending ({docs_pending}) <= threshold ({THRESHOLD}), done!")
        break

print("\nAll rounds complete.")
