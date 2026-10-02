"""B4 AI 噪声清理（按资产分组）— 自动循环。"""
from __future__ import annotations

import subprocess
import sys
import re
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SCRIPT = SCRIPT_DIR / "phase_b2_ai_noise_clean_by_asset.py"

MAX_ROUNDS = 100
ASSETS_PER_ROUND = 20
# 单轮超时（每个资产约 30s LLM + 抓取，20 个资产留 20 分钟余量）
ROUND_TIMEOUT_SECONDS = 20 * 60
# 总时长上限（避免跑一整天占着调度槽位，默认 4 小时）
TOTAL_MAX_SECONDS = 4 * 3600


def _run_round(rnd: int) -> subprocess.CompletedProcess:
    """跑一轮，子进程 stdout/stderr 实时透传到本进程 stdout。

    审计 2026-10-02 P1-5：此前用 `capture_output=True` 吞掉子进程全部输出，
    而一轮要连续跑 20 个资产的 LLM 判断（期间父进程不打印任何日志）——
    调度器看护按「240 分钟无新日志」判卡死，把整个任务杀掉（实测 28 次全
    failed，error 均为 `stuck: 240分钟无新日志`）。改为 Popen + 读线程逐行
    透传：子进程每打印一行，父进程即时转发，看护始终能看到活跃日志；同时
    保留超时与退出码语义（用读线程收集输出，主线程轮询 wait 判超时）。
    """
    proc = subprocess.Popen(
        [PYTHON, "-u", str(SCRIPT), "--execute", "--limit", str(ASSETS_PER_ROUND)],
        cwd=str(SCRIPT_DIR.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    out_lines: list[str] = []
    stop_reader = threading.Event()

    def _drain():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out_lines.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    try:
        rc = proc.wait(timeout=ROUND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        print(f"  ⚠️  本轮超时（>{ROUND_TIMEOUT_SECONDS}s），跳过继续下一轮")
        return subprocess.CompletedProcess(
            [PYTHON, str(SCRIPT)], 1, "".join(out_lines), "timeout"
        )
    finally:
        stop_reader.set()
        reader.join(timeout=5)
    return subprocess.CompletedProcess(
        [PYTHON, str(SCRIPT)], rc, "".join(out_lines), ""
    )


def main():
    start_time = time.time()

    for rnd in range(1, MAX_ROUNDS + 1):
        # 总时长检查
        elapsed = time.time() - start_time
        if elapsed >= TOTAL_MAX_SECONDS:
            print(f"\n已运行 {elapsed/3600:.1f}h，达到总时长上限，停止。")
            break

        print(f"\n{'=' * 60}")
        print(f"  Round {rnd} / max {MAX_ROUNDS}  |  每轮 {ASSETS_PER_ROUND} 个资产"
              f"  |  已运行 {elapsed/60:.0f}min")
        print(f"{'=' * 60}")

        cp = _run_round(rnd)
        if cp.stderr:
            print(cp.stderr, file=sys.stderr)

        # 检查是否还有剩余资产
        # 从输出中提取：处理资产: X 个
        m = re.search(r"处理资产:\s*(\d+)\s*个", cp.stdout or "")
        if m and int(m.group(1)) == 0:
            print("全部完成。")
            break

        if cp.returncode != 0:
            print(f"本轮出错，退出 (rc={cp.returncode})")
            break

    print("自动循环结束。")


if __name__ == "__main__":
    main()