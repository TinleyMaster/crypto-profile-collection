"""
SPA 无头浏览器爬取自动循环脚本。
持续处理 needs_browser=TRUE 的条目直到全部完成。
直接导入调用 phase_b2_spa_browser_crawl.main()，消除子进程管道问题。
"""

import concurrent.futures
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from phase_b2_spa_browser_crawl import main as crawl_main

BATCH_LIMIT = 20
MAX_ROUNDS = 100
TIMEOUT = 300  # 5 分钟（留给 Chromium 启动 + 串行爬取 20 页）
MAX_CONSECUTIVE_ERRORS = 3

total_discovered = 0
consecutive_errors = 0

for round_num in range(1, MAX_ROUNDS + 1):
    print(f"\n{'=' * 60}")
    print(f"  Round {round_num} / max {MAX_ROUNDS}  |  batch={BATCH_LIMIT}  累计发现={total_discovered} 链接")
    print(f"{'=' * 60}")

    # 设 sys.argv 让 crawl_main 解析参数
    old_argv = sys.argv
    sys.argv = [
        "phase_b2_spa_browser_crawl.py",
        "--limit", str(BATCH_LIMIT),
        "--concurrency", "4",
    ]

    def _run():
        try:
            return crawl_main()
        except SystemExit as e:
            return e.code or 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as timeout_executor:
        future = timeout_executor.submit(_run)
        try:
            exit_code = future.result(timeout=TIMEOUT)
        except concurrent.futures.TimeoutError:
            print(f"\n本轮超时（{TIMEOUT // 60}分钟），跳过继续。")
            consecutive_errors += 1
            print(f"本轮超时（{TIMEOUT // 60}分钟），连续失败: {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}")
            sys.argv = old_argv
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print("连续失败过多，自动停止。")
                break
            continue
        finally:
            sys.argv = old_argv

    if exit_code != 0:
        print(f"exit code {exit_code}")
        consecutive_errors += 1
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print("连续失败过多，自动停止。")
            break
        continue

    consecutive_errors = 0

    # crawl_main 的 main() 最后打印 JSON 摘要到 stdout，
    # 但我们没法在这里解析（stdout 没被捕获）。
    # 依赖 main() 内部的进度输出即可。
    # 如果 main() 返回 0 但没有 candidates，它会自己打印 "no_candidates" 然后 return 0。
    # 此时下一轮查询也会返回 0 candidates，形成自然循环但不会新增。
    # 
    # 简单判断：如果 exit_code == 0 且 candidates 为 0，main() 内部会打印 JSON，
    # 我们在控制台能看到。继续循环也可以，反正下轮还是 0 candidates 又会快速返回。

    # 检查是否还有候选（查 DB）
    import psycopg
    import psycopg.rows
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    try:
        settings = get_settings(require_database=True)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM biz.doc_source_entry WHERE needs_browser = TRUE LIMIT 1"
                )
                has_more = cur.fetchone() is not None
        if not has_more:
            print("\n无更多 SPA 页面待处理，全部完成！")
            break
    except Exception as e:
        print(f"检查剩余候选失败: {e}")

print(f"\nAll rounds complete.  累计发现: {total_discovered} 链接")
