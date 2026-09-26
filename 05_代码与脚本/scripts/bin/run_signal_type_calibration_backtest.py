#!/usr/bin/env python3
"""
signal_type 级回测校准 · 周级重算（刀2 复验 47bcb4d §五 #5）。

biz.signal_type_calibration 此前是「一次性快照」，只能人工重跑；校准窗口会随样本
陈旧而失真（macro_market 消费端按 window_end DESC 取最新窗口）。本脚本把
`workbench/backtest_opportunities.py --days N --write` 纳入调度，周级重算。

幂等：落表键 = (signal_type, horizon_days, window_end)，同窗口重跑是 upsert，
不会产生重复行；因此偏短周期重跑也安全。

用法：
    python run_signal_type_calibration_backtest.py            # 默认回测 30 天
    python run_signal_type_calibration_backtest.py --days 60
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下，Dockerfile 扁平拷贝）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="signal_type 回测校准周级重算")
    parser.add_argument("--days", type=int, default=30, help="回测天数（默认30）")
    args = parser.parse_args()

    target = WORKBENCH_DIR / "backtest_opportunities.py"
    if not target.exists():
        print(f"[ERR] 未找到回测脚本：{target}", flush=True)
        return 2

    cmd = [sys.executable, "-u", str(target), "--days", str(args.days), "--write"]
    print(f"[CMD] {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())