#!/usr/bin/env python3
"""
投研结论前向跟踪回填（biz.thesis_forward_track）。

P4（2026-09-27，方案 §1.3/§5）：投研页三档确定性的闸门阈值当前无任何前向样本可校准
（backtest_opportunities.py 回测对象是 market_overview_snapshot 的 opportunity，不是
biz.research_thesis）。本脚本每天把已到期的跟踪行按 T+7/30/90 回填前向收益，积累样本后
即可按 gate_s_open / gate_m_open 分组比较收益，验证闸门区分度。

逻辑全部在 db_stats.backfill_thesis_forward_track()（幂等）：
  · 只填「已到期 且 该期次为空 且 price_at 非空」的格，已填不重算；
  · 三期全部填满 → 置 filled_at（离开到期扫描索引）；
  · price_at 为空的行三期均跳过（无基准价算不出收益，不写假数）。

用法：
    python backfill_thesis_forward_track.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
if str(WORKBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def main() -> int:
    from db_stats import backfill_thesis_forward_track  # noqa: E402

    stats = backfill_thesis_forward_track(log=print)
    print("=" * 60)
    print(f"扫描未完成跟踪行: {stats['scanned']}")
    print(f"填入收益格数:     {stats['filled_cells']}")
    print(f"三期完成行数:     {stats['rows_completed']}")
    print(f"无基准价跳过行数: {stats['skipped_no_price']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())