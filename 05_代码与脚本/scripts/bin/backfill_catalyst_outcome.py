#!/usr/bin/env python3
"""催化剂 outcome 分批回填 runner：避免单连接超长被 Zeabur 回收。

每次调用 collect_catalyst_outcome.py --limit BATCH，直到无可处理信号。
"""
import subprocess
import sys
from pathlib import Path

BATCH = 300
SCRIPT = Path(__file__).resolve().parent / "collect_catalyst_outcome.py"


def main() -> int:
    total_done = 0
    for round_no in range(1, 30):  # 上限 30 轮防死循环
        print(f"\n===== 批次 {round_no} (limit={BATCH}) =====", flush=True)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--limit", str(BATCH)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(proc.stdout)
        if proc.stderr.strip():
            print("STDERR:", proc.stderr[-2000:], flush=True)
        # 解析 "完成: ... 结算/更新 N 条"
        done = 0
        for line in proc.stdout.splitlines():
            if "完成:" in line:
                try:
                    done = int(line.split("结算/更新")[1].split("条")[0].strip())
                except Exception:
                    done = 0
        if done == 0:
            print("本轮无新增结算，停止。", flush=True)
            break
        total_done += done
        if proc.returncode != 0:
            print(f"本轮退出码 {proc.returncode}，重试下一轮（幂等续跑）", flush=True)
    print(f"\n全部完成，累计结算 {total_done} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
