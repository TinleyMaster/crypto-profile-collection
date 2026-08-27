#!/usr/bin/env python3
"""
催化剂数据摄入调度 wrapper。

调用 workbench/catalyst/run_catalyst.py 跑所有已注册源（增量模式）。
调度器从 scripts/bin/ 执行，本脚本桥接到 workbench 下的催化剂模块。
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
# 优先用 /app/workbench，不存在则用 /app（prod 扁平部署）
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent

# 确保能 import catalyst 包 和 crypto_research
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from catalyst.runner import run_all  # noqa: E402


def main() -> int:
    print("=" * 60, flush=True)
    print("催化剂数据摄入（全源增量）", flush=True)
    print("=" * 60, flush=True)

    results = run_all()

    print(flush=True)
    print("=" * 60, flush=True)
    print("摄入完成", flush=True)
    print("=" * 60, flush=True)
    print(f"{'源':<30} {'抓取':>6} {'新增':>6} {'合并':>6} {'跳过':>6}  错误", flush=True)
    print("-" * 70, flush=True)

    total_fetched = 0
    total_inserted = 0
    total_errors = 0

    for r in results:
        print(
            f"{r['source_code']:<30} "
            f"{r['fetched']:>6} "
            f"{r['inserted']:>6} "
            f"{r['merged']:>6} "
            f"{r['skipped']:>6}  "
            f"{r['error']}",
            flush=True,
        )
        total_fetched += r["fetched"]
        total_inserted += r["inserted"]
        if r["error"]:
            total_errors += 1

    print(flush=True)
    print(f"总计: 抓取 {total_fetched} 条, 新增 {total_inserted} 条, 错误源 {total_errors}/{len(results)}", flush=True)

    # 所有源都报错才算失败；部分源失败但有数据进来仍算成功
    if total_errors == len(results) and total_fetched == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
