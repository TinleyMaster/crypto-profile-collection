#!/usr/bin/env python3
"""
外部交易所上新公告摄入入口（OKX / Bitget / Bybit）。

背景（2026-10-16 数据源诊断）：
    原催化剂源 99.8% 来自币安（KOL 转述 + 币安 CMS），**零外部交易所源**。
    三家交易所公告是「一手、免费、免 key、分钟级、标题自带 symbol」的最强上新
    催化剂，可经 linker 直接落 asset_id（实测归属率 83%，远高于现有全局口径）。

与 catalyst_ingest_all.py 的关系：
    后者跑**全源**（含币安），由 catalyst_run_all 每 12h 触发；
    本脚本**只跑这三个源**、以更高频（每 30 分钟）独立调度 —— 上新公告对时延敏感，
    12h 太慢；且高频只打 3 个轻量端点，压力可忽略。

用法：
    python ingest_exchange_announcements.py [--max-pages 1]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod 结构: /app/scripts/bin/ → /app/（workbench 文件直接在 /app/ 下）
# 本地结构: .../scripts/bin/ → .../workbench/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent

sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from catalyst.runner import run_source  # noqa: E402

# 只含加密上新 + 下架；上/下架分 source_code 便于分别取证
SOURCES = [
    "okx_announcement",
    "bitget_announcement",
    "bybit_announcement",
    "okx_delisting",
    "bybit_delisting",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=1, help="每源最多翻页数（增量 1 页足够）")
    args = ap.parse_args()

    print("=" * 60, flush=True)
    print("外部交易所公告摄入（OKX / Bitget / Bybit）", flush=True)
    print("=" * 60, flush=True)
    print(f"{'源':<24} {'抓取':>6} {'新增':>6} {'合并':>6} {'跳过':>6}  错误", flush=True)
    print("-" * 70, flush=True)

    total_fetched = total_inserted = total_errors = 0
    for code in SOURCES:
        r = run_source(code, max_pages=args.max_pages)
        print(
            f"{r['source_code']:<24} {r['fetched']:>6} {r['inserted']:>6} "
            f"{r['merged']:>6} {r['skipped']:>6}  {r['error']}",
            flush=True,
        )
        total_fetched += r["fetched"]
        total_inserted += r["inserted"]
        if r["error"]:
            total_errors += 1

    print(flush=True)
    print(
        f"总计: 抓取 {total_fetched} 条, 新增 {total_inserted} 条, "
        f"错误源 {total_errors}/{len(SOURCES)}",
        flush=True,
    )
    # 所有源都报错才算失败（部分源失败但有数据仍算成功）
    if total_errors == len(SOURCES) and total_fetched == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
