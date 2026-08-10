"""
SPA 回溯扫描自动循环脚本。
持续扫描 B2 已爬取但未标记 needs_browser 的页面，识别 SPA 并标记。
"""

import subprocess
import sys
import os
import threading
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 500
WORKERS = 4
MAX_ROUNDS = 100
TIMEOUT = 600  # 10 分钟

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_spa = 0


def run_with_streaming(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Popen + 实时流式输出，返回 (returncode, stdout_text, stderr_text)。"""
    stdout_lines = []
    stderr_lines = []

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def read_stdout():
        for line in iter(proc.stdout.readline, ""):
            print(line, end="")
            stdout_lines.append(line)

    def read_stderr():
        for line in iter(proc.stderr.readline, ""):
            stderr_lines.append(line)

    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(f"\n本轮超时（{timeout // 60}分钟），跳过继续。")
        return -1, "".join(stdout_lines), "".join(stderr_lines)

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    return proc.returncode, "".join(stdout_lines), "".join(stderr_lines)


for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计 SPA={total_spa}")
    print(f"{'=' * 60}")

    returncode, stdout_text, stderr_text = run_with_streaming(
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
        timeout=TIMEOUT,
    )

    if returncode == -1:  # 超时
        continue

    if returncode != 0:
        print(f"Script exited with code {returncode}, stopping.")
        if stderr_text:
            print(f"stderr: {stderr_text[:500]}")
        break

    # 从输出中提取 SPA 数量
    spa_count = 0
    for line in stdout_text.splitlines():
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

    if "无候选" in stdout_text:
        break

    print(f"  本轮 SPA: {spa_count}")

print(f"\nAll rounds complete.  累计发现 SPA: {total_spa}")