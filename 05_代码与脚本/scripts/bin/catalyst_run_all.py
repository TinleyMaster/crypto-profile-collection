#!/usr/bin/env python3
"""催化剂全链路合并任务：摄入 → AI 预处理 → thesis 重生。

替代原 4 个独立工作台子任务（catalyst_ingest_all / catalyst_ai_process /
catalyst_thesis_regen / catalyst_thesis_regen_cursor）。单次运行完成整条管道。

容错策略：各阶段独立 subprocess 调用，任一阶段非 0 退出不中断后续阶段
（与 ingest 自身"部分源失败仍成功"的哲学一致）；最终退出码取各阶段最高非 0 值。
"""
import subprocess
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent

STAGES = [
    ("全源增量摄入", "catalyst_ingest_all.py", []),
    ("AI 预处理", "process_catalyst_ai.py", ["--batch-size", "200"]),
    ("thesis 重生(游标模式)", "catalyst_thesis_regen.py", ["--max-assets", "100"]),
]


def main() -> int:
    print("=" * 60, flush=True)
    print("催化剂全链路（摄入 → AI → thesis）", flush=True)
    print("=" * 60, flush=True)

    worst_rc = 0
    for label, script, extra in STAGES:
        cmd = [sys.executable, "-u", str(BIN_DIR / script)] + extra
        print(f"\n>>> [{label}] {script} {' '.join(extra)}", flush=True)
        rc = subprocess.run(cmd).returncode
        print(f"<<< [{label}] 退出码={rc}", flush=True)
        if rc != 0:
            worst_rc = max(worst_rc, rc)
            print(f"⚠️  阶段 [{label}] 非 0 退出，继续执行后续阶段", flush=True)

    print("\n" + "=" * 60, flush=True)
    print(f"催化剂全链路结束，整体退出码={worst_rc}", flush=True)
    print("=" * 60, flush=True)
    return worst_rc


if __name__ == "__main__":
    sys.exit(main())