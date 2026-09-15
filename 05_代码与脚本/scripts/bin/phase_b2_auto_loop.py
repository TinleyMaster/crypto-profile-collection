"""
B2 自动循环脚本：持续运行 phase_b2_deep_doc_discovery 直到 docs 类型 pending 降到阈值以下。
"""

import subprocess
import sys
import os
import threading
import time
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

    # 运行 B2（Popen + 心跳：子进程卡住无输出时定期打印心跳，
    # 避免 90 分钟无日志被 TaskManager 误判为 stuck 收割）
    b2_cmd = [
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
    ]
    try:
        proc = subprocess.Popen(
            b2_cmd,
            cwd=str(SCRIPT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as e:
        print(f"B2 启动失败: {e}")
        break

    def _pump() -> None:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if line:
                print(f"  [B2] {line}", flush=True)

    threading.Thread(target=_pump, daemon=True).start()

    # 轮询：每 60s 打心跳，超过 30 分钟无结束则 kill（原超时语义）
    round_start = time.time()
    last_beat = time.time()
    rc = None
    while time.time() - round_start < 1800:
        if proc.poll() is not None:
            rc = proc.returncode
            break
        if time.time() - last_beat >= 60:
            print(f"  [B2] 运行中 {int(time.time() - round_start)}s ...（心跳）", flush=True)
            last_beat = time.time()
        time.sleep(5)

    if rc is None:
        proc.kill()
        proc.wait()
        print("B2 脚本本轮超时（30分钟），跳过继续下一轮。")
        continue

    result = proc
    if result.returncode != 0:
        print(f"Script exited with code {result.returncode}, stopping.")
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
