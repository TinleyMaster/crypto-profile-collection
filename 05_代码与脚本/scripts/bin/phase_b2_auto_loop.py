"""
B2 自动循环脚本：持续运行 phase_b2_deep_doc_discovery 直到 docs 类型 pending 降到阈值以下。
"""

import subprocess
import sys
import os
import signal
from pathlib import Path

# 行缓冲：确保 print 实时输出（stdout 是 pipe 时默认全缓冲）
sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 1000
MAX_ROUNDS = 200  # 安全上限
THRESHOLD = 500  # 全部可爬类型 pending 低于此数时停止

# 新入库币水位线：仅处理 asset_id >= 该值的资产，老语料（已成熟）跳过。
# 取值 = 语料成熟时刻的 MAX(asset_id)，可用环境变量 MIN_NEW_ASSET_ID 覆盖。
MIN_ASSET_ID = int(os.environ.get("MIN_NEW_ASSET_ID", "25952"))

# 设置环境
env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}")
    print(f"{'=' * 60}")

    # 运行 B2
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT_DIR / "phase_b2_deep_doc_discovery.py"),
                "--limit",
                str(BATCH_LIMIT),
                "--min-asset-id",
                str(MIN_ASSET_ID),
                "--workers",
                "4",
                "--timeout",
                "8",
            ],
            cwd=str(SCRIPT_DIR),
            env=env,
            start_new_session=True,  # 独立进程组，超时可连孙进程一起强杀
        )
        try:
            rc = proc.wait(timeout=900)  # 单轮最长 15 分钟
        except subprocess.TimeoutExpired:
            # 连进程组一起强杀，防止浏览器/爬虫 worker（孙进程）泄漏导致 watchdog 误判卡死
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            print("B2 脚本本轮超时（15分钟），已强杀进程组，继续下一轮。")
            continue
    except Exception as e:
        print(f"B2 启动失败: {e}，继续下一轮。")
        continue

    if rc != 0:
        print(f"Script exited with code {rc}, stopping.")
        break

    print("B2 本轮完成，查询 pending 数量...")

    # 检查 pending
    try:
        probe = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "_probe_b2_pending.py"),
                "--min-asset-id",
                str(MIN_ASSET_ID),
            ],
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

    # 解析 Total pending count
    total_pending = None
    for line in probe.stdout.splitlines():
        if line.startswith("Total pending:"):
            total_pending = int(line.split(":")[1].strip())

    print(probe.stdout.strip())

    if total_pending is not None and total_pending <= THRESHOLD:
        print(f"\nTotal pending ({total_pending}) <= threshold ({THRESHOLD}), done!")
        break

    if total_pending is not None:
        print(f"Total pending ({total_pending}) > threshold ({THRESHOLD})，继续下一轮...")

print("\nAll rounds complete.")
