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
PROJECT_ROOT = SCRIPT_DIR.parent.parent
WORKBENCH_DIR = PROJECT_ROOT / "workbench"

# 确保能 import catalyst 包
sys.path.insert(0, str(WORKBENCH_DIR))

from catalyst.runner import run_all  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("催化剂数据摄入（全源增量）")
    print("=" * 60)

    results = run_all()

    print()
    print("=" * 60)
    print("摄入完成")
    print("=" * 60)
    print(f"{'源':<30} {'抓取':>6} {'新增':>6} {'合并':>6} {'跳过':>6}  错误")
    print("-" * 70)

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
            f"{r['error']}"
        )
        total_fetched += r["fetched"]
        total_inserted += r["inserted"]
        if r["error"]:
            total_errors += 1

    print()
    print(f"总计: 抓取 {total_fetched} 条, 新增 {total_inserted} 条, 错误源 {total_errors}/{len(results)}")

    # 所有源都报错才算失败；部分源失败但有数据进来仍算成功
    if total_errors == len(results) and total_fetched == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
