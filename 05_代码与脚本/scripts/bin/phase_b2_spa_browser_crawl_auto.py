"""
SPA 无头浏览器爬取自动循环脚本。
持续处理 needs_browser=TRUE 的条目直到全部完成。
"""

import subprocess
import sys
import os
import threading
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 20
MAX_ROUNDS = 100
BROWSER_CONCURRENCY = 4
TIMEOUT = 300  # 5 分钟（子进程内部已有 BATCH_TIMEOUT=300）

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

total_entries = 0
total_discovered = 0
consecutive_timeouts = 0
MAX_CONSECUTIVE_TIMEOUTS = 3


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
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计发现={total_discovered} 链接")
    print(f"{'=' * 60}")

    returncode, stdout_text, stderr_text = run_with_streaming(
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
        timeout=TIMEOUT,
    )

    if returncode == -1:  # 超时
        consecutive_timeouts += 1
        print(f"本轮超时（{TIMEOUT // 60}分钟），连续超时: {consecutive_timeouts}/{MAX_CONSECUTIVE_TIMEOUTS}")
        if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            print(f"连续超时 {MAX_CONSECUTIVE_TIMEOUTS} 次，自动停止。")
            break
        continue

    # 成功一轮则重置超时计数
    consecutive_timeouts = 0

    if returncode != 0:
        print(f"Script exited with code {returncode}, stopping.")
        if stderr_text:
            print(f"stderr: {stderr_text[:500]}")
        break

    import json
    summary = None
    for line in reversed(stdout_text.strip().splitlines()):
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