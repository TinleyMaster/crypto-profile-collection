#!/usr/bin/env python
"""
KOL 信号监控 — 调度器兜底脚本。

由 scheduler.py 每 5 分钟触发一次，作为 kol_daemon.py 常驻进程的兜底。
正常情况下 kol_daemon.py 以 30 秒间隔常驻运行，此脚本只在常驻进程挂掉时兜底。

用法：
    python kol_monitor_run.py --run-once
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 找到 workbench 目录
if os.path.exists("/app"):
    WORKBENCH_DIR = Path("/app")
else:
    WORKBENCH_DIR = Path(__file__).resolve().parents[2] / "workbench"

sys.path.insert(0, str(WORKBENCH_DIR))

from kol.runner import run_crawl_once  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="KOL 信号监控（调度器兜底）")
    parser.add_argument("--run-once", action="store_true", help="只跑一次")
    parser.add_argument("--platform", type=str, default=None, help="指定平台")
    args = parser.parse_args()

    stats = run_crawl_once(platform_code=args.platform)
    print(f"KOL 监控完成: 新帖 {stats['posts_new']}, "
          f"信号 {stats['signals_created']}, 告警 {stats['alerts_sent']}")
    if stats["errors"]:
        print(f"错误: {stats['errors']}")


if __name__ == "__main__":
    main()
