"""
KOL 信号监控守护进程。

独立于 scheduler.py 运行，使用币安广场公开 HTTP API（免认证）抓取。

设计：
  - 常驻进程，循环执行抓取 → AI 分类 → 邮件提醒
  - 每轮间隔由 KOL_POLL_INTERVAL 环境变量控制（默认 900 秒 = 15 分钟）
  - 单轮失败不影响下一轮
  - 支持 --run-once 只跑一次（调试用）

用法：
    python kol_daemon.py                 # 前台常驻
    python kol_daemon.py --run-once      # 只跑一次
    python kol_daemon.py --interval 600  # 自定义间隔（秒）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

# 把 workbench 目录加入 path
WORKSPACE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from kol.runner import run_crawl_once, run_discover_once  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="KOL 信号监控守护进程")
    parser.add_argument("--run-once", action="store_true",
                        help="只跑一次（调试用）")
    parser.add_argument("--interval", type=int, default=None,
                        help="轮询间隔秒数（默认读取环境变量 KOL_POLL_INTERVAL，默认 900）")
    parser.add_argument("--platform", type=str, default=None,
                        help="只监控指定平台")
    parser.add_argument("--headed", action="store_true",
                        help="有头模式（调试用，API 模式无实际作用）")
    parser.add_argument("--no-discover", action="store_true",
                        help="禁用每日博主发现（默认启用，每天 0 点左右执行一次）")
    parser.add_argument("--discover-pages", type=int, default=10,
                        help="每日发现：翻页数量（默认 10）")
    parser.add_argument("--discover-min-followers", type=int, default=10000,
                        help="每日发现：最低粉丝数（默认 10000）")
    args = parser.parse_args()

    interval = args.interval
    if interval is None:
        interval = int(os.getenv("KOL_POLL_INTERVAL", "900"))

    print(f"[KOL][daemon] 启动，轮询间隔 {interval}s，平台: {args.platform or '全部'}")
    if not args.no_discover:
        print(f"[KOL][daemon] 每日博主发现已启用（min_followers={args.discover_min_followers}）")

    if args.run_once:
        run_crawl_once(
            platform_code=args.platform,
            headless=not args.headed,
        )
        return

    # 常驻循环
    round_count = 0
    last_discover_date = None  # 记录上次发现日期，确保每天只跑一次
    while True:
        round_count += 1
        start = time.time()
        print(f"\n[KOL][daemon] === 第 {round_count} 轮开始 ===")

        try:
            stats = run_crawl_once(
                platform_code=args.platform,
                headless=not args.headed,
            )
            elapsed = time.time() - start
            print(f"[KOL][daemon] 第 {round_count} 轮完成，耗时 {elapsed:.1f}s，"
                  f"新帖 {stats['posts_new']}，信号 {stats['signals_created']}，"
                  f"告警 {stats['alerts_sent']}")
        except Exception as e:
            print(f"[KOL][daemon] 第 {round_count} 轮异常: {e}")
            traceback.print_exc()

        # 每日博主发现（每天一次，在抓取轮次之间穿插执行）
        if not args.no_discover:
            from datetime import datetime
            today = datetime.now().date().isoformat()
            if last_discover_date != today:
                try:
                    print(f"[KOL][daemon] 执行每日博主发现...")
                    disc_stats = run_discover_once(
                        platform_code=args.platform or "binance_square",
                        max_pages=args.discover_pages,
                        min_followers=args.discover_min_followers,
                        auto_activate=False,
                    )
                    last_discover_date = today
                    print(f"[KOL][daemon] 每日发现完成：新增 {disc_stats['new']}，"
                          f"更新 {disc_stats['updated']}")
                except Exception as e:
                    print(f"[KOL][daemon] 每日发现异常: {e}")
                    traceback.print_exc()

        # 计算休眠时间（扣除本轮耗时）
        elapsed = time.time() - start
        sleep_time = max(1, interval - elapsed)
        print(f"[KOL][daemon] 休眠 {sleep_time:.0f}s...")
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
