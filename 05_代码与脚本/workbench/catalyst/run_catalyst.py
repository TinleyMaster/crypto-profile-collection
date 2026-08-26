"""
催化剂统一入口脚本。

用法：
    # 跑所有已注册的源（增量模式）
    python run_catalyst.py

    # 跑指定源
    python run_catalyst.py --source binance_square_news
    python run_catalyst.py --source binance_news --source binance_listing

    # 全量重抓（指定页数）
    python run_catalyst.py --source binance_square_news --max-pages 10

    # 列出所有已注册的源
    python run_catalyst.py --list
"""
from __future__ import annotations

import sys
import os
import argparse
import logging

# 确保能 import catalyst 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from catalyst import run_source, run_all, list_sources  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="催化剂数据抓取（多平台统一入口）")
    parser.add_argument(
        "--source", "-s",
        action="append",
        help="指定源编码（可多次指定），默认跑全部",
    )
    parser.add_argument(
        "--max-pages", "-p",
        type=int,
        default=None,
        help="覆盖默认翻页数",
    )
    parser.add_argument(
        "--since",
        type=float,
        default=None,
        help="指定增量起点（秒级时间戳），默认自动取库中最新",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="列出所有已注册的源",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.list:
        sources = list_sources()
        print(f"\n已注册的催化剂源（{len(sources)} 个）：")
        for s in sources:
            print(f"  - {s}")
        print()
        return

    sources = args.source  # None 表示全部

    print(f"\n{'='*60}")
    print(f"催化剂抓取开始")
    print(f"  源: {', '.join(sources) if sources else '全部'}")
    print(f"  翻页数: {args.max_pages or '默认'}")
    print(f"  增量起点: {args.since or '自动'}")
    print(f"{'='*60}\n")

    results = run_all(sources=sources, since_ts=args.since, max_pages=args.max_pages)

    print(f"\n{'='*60}")
    print(f"抓取完成")
    print(f"{'='*60}")
    print(f"{'源':<30} {'抓取':>6} {'新增':>6} {'合并':>6} {'跳过':>6}  错误")
    print("-" * 70)
    for r in results:
        print(
            f"{r['source_code']:<30} "
            f"{r['fetched']:>6} "
            f"{r['inserted']:>6} "
            f"{r['merged']:>6} "
            f"{r['skipped']:>6}  "
            f"{r['error']}"
        )
    print()


if __name__ == "__main__":
    main()
