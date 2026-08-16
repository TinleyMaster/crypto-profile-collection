"""
third_party raises 自动循环脚本：持续运行 phase_b2_third_party_raises 直到无剩余候选。

每次调用 phase_b2_third_party_raises.py 会：
- 按 biz.dl_protocol_checked 缺失重新选择候选（天然断点续跑）
- 逐资产写入并提交，进度按轮保存

对数据库瞬时断连（Zeabur 偶发 server closed the connection）做带退避的重试。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_LIMIT = 50
MAX_ROUNDS = 200  # 安全上限（约 8062 候选 / 50 ≈ 161 轮，留足余量）
ROUND_TIMEOUT = 1800  # 单轮最长 30 分钟
MAX_ATTEMPTS = 4  # 单轮最多尝试次数（含首次）
RETRY_DELAY = 15  # 失败重试间隔（秒）
FAILED_ROUND_BACKOFF = 60  # 整轮失败后的退避（秒）

# 本地开发时用项目 venv 的 site-packages 兜底，使系统 python3 也能 import psycopg；
# 云端(Docker) 已安装 psycopg，且该目录不存在，自动跳过。
_PROJECT_ROOT = SCRIPT_DIR.parents[2]
VENV_SITE = ""
for _pyver in ("3.13", "3.11", "3.10"):
    _candidate = _PROJECT_ROOT / ".venv" / "lib" / f"python{_pyver}" / "site-packages"
    if _candidate.is_dir():
        VENV_SITE = str(_candidate)
        break

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"
if VENV_SITE and VENV_SITE not in env.get("PYTHONPATH", ""):
    env["PYTHONPATH"] = VENV_SITE + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")


def _tail(lines: list[str], n: int = 8) -> str:
    return "\n".join(lines[-n:])


def _run_round() -> subprocess.CompletedProcess | None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            print(f"  重试 {attempt - 1}/{MAX_ATTEMPTS - 1}，等待 {RETRY_DELAY}s ...")
            time.sleep(RETRY_DELAY)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "phase_b2_third_party_raises.py"),
                    "--limit",
                    str(BATCH_LIMIT),
                ],
                cwd=str(SCRIPT_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=ROUND_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print("  本轮超时（30 分钟），重试。")
            continue
        if result.returncode == 0:
            return result
        print(f"  脚本退出码 {result.returncode}，重试。")
        if result.stderr:
            print("  stderr:", _tail((result.stderr or "").splitlines(), 3))
    return None


for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}")
    print(f"{'=' * 60}")

    result = _run_round()
    if result is None:
        print(f"  本轮 {MAX_ATTEMPTS} 次尝试均失败，退避 {FAILED_ROUND_BACKOFF}s 后进入下一轮。")
        time.sleep(FAILED_ROUND_BACKOFF)
        continue

    stdout_lines = (result.stdout or "").splitlines()
    print(_tail(stdout_lines))
    if result.stderr:
        print("stderr:", _tail((result.stderr or "").splitlines(), 4))

    # 最后一行为 JSON 结果，检测是否已无候选
    last_json = next(
        (ln.strip() for ln in reversed(stdout_lines) if ln.strip().startswith("{")),
        "",
    )
    if '"no_candidates"' in last_json:
        print("\n无剩余候选，全部完成。")
        break

print("\nAll rounds complete.")
