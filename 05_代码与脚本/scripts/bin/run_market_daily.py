"""大盘数据日频总控：串起 4 个数据源采集 + 快照生成，保证依赖顺序。

设计：
  - 串行执行：恐贪 → BTC OI → CEFI → 赛道 TVL → 快照生成
  - 单个失败不中断后续任务，记录错误后继续
  - 每个子任务独立 try/except，异常信息汇总到结果报告
  - 最终输出执行摘要（成功/失败/耗时）

用法：
    python run_market_daily.py              # 正常运行
    python run_market_daily.py --dry-run    # 预览，不写库
    python run_market_daily.py --no-raw     # 快照不存完整 JSON
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Windows 管道默认 GBK，显式指定 UTF-8 避免 ✓/✗ 等符号抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# 任务定义：(key, 脚本名, 描述, 默认参数列表)
TASKS = [
    ("fear_greed", "ingest_fear_greed.py", "恐贪指数日频采集", []),
    ("btc_oi", "ingest_btc_oi.py", "BTC 未平仓合约日频采集", []),
    ("cefi_index", "ingest_cefi_index.py", "CEFI 指数日频采集", []),
    ("category_tvl", "ingest_category_tvl.py", "赛道 TVL 日频快照", []),
    ("market_snapshot", "build_market_snapshot.py", "大盘每日快照落库", []),
]


def run_task(script_name: str, extra_args: list[str]) -> tuple[bool, float, str]:
    """执行一个子脚本，返回 (成功, 耗时秒, 输出摘要)。"""
    script_path = SCRIPT_DIR / script_name
    cmd = [sys.executable, "-u", str(script_path)] + extra_args

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 单任务 10 分钟超时
            cwd=str(SCRIPT_DIR.parent.parent),  # workbench 目录，与其他脚本一致
            env=None,  # 继承当前环境
        )
        elapsed = time.time() - start

        if proc.returncode == 0:
            tail = proc.stdout.strip().splitlines()[-5:] if proc.stdout.strip() else []
            summary = "OK | " + (" | ".join(tail[-2:]) if tail else "no output")
            return True, elapsed, summary
        else:
            err_tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
            summary = f"FAIL (exit {proc.returncode}) | " + " | ".join(err_tail[-2:])
            return False, elapsed, summary

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return False, elapsed, "TIMEOUT (600s)"
    except Exception as e:
        elapsed = time.time() - start
        return False, elapsed, f"ERROR: {e}"


def main() -> None:
    parser = argparse.ArgumentParser(description="大盘数据日频总控（采集 + 快照）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写库")
    parser.add_argument("--no-raw", action="store_true", help="快照不存完整 JSON")
    parser.add_argument("--skip", action="append", default=[],
                        help="跳过指定任务 key，可多次指定（如 --skip category_tvl）")
    args = parser.parse_args()

    extra_snapshot_args = []
    if args.dry_run:
        extra_snapshot_args.append("--dry-run")
    if args.no_raw:
        extra_snapshot_args.append("--no-raw")

    print("=" * 60)
    print("[market_daily] 大盘数据日频总控启动")
    print(f"[market_daily] dry_run={args.dry_run}, no_raw={args.no_raw}, skip={args.skip}")
    print("=" * 60)

    results: list[dict] = []
    total_start = time.time()

    for key, script, desc, default_args in TASKS:
        if key in args.skip:
            print(f"\n>>> [{key}] {desc} — SKIPPED")
            results.append({"key": key, "status": "skipped", "elapsed": 0, "summary": "用户跳过"})
            continue

        extra = extra_snapshot_args if key == "market_snapshot" else []
        if args.dry_run and key != "market_snapshot":
            # 采集脚本也支持 dry-run 的话传进去
            extra.append("--dry-run")

        task_args = default_args + extra
        print(f"\n>>> [{key}] {desc}")
        print(f"    脚本: {script} {' '.join(task_args)}")

        ok, elapsed, summary = run_task(script, task_args)
        status = "ok" if ok else "fail"
        print(f"    结果: {status.upper()} ({elapsed:.1f}s)")
        print(f"    摘要: {summary}")

        results.append({
            "key": key,
            "status": status,
            "elapsed": round(elapsed, 1),
            "summary": summary,
        })

    total_elapsed = time.time() - total_start
    ok_count = sum(1 for r in results if r["status"] == "ok")
    fail_count = sum(1 for r in results if r["status"] == "fail")
    skip_count = sum(1 for r in results if r["status"] == "skipped")

    print("\n" + "=" * 60)
    print("[market_daily] 执行完成")
    print(f"  总耗时: {total_elapsed:.1f}s")
    print(f"  成功: {ok_count}  |  失败: {fail_count}  |  跳过: {skip_count}")
    for r in results:
        icon = {"ok": "✓", "fail": "✗", "skipped": "○"}.get(r["status"], "?")
        print(f"  {icon} {r['key']:15s} {r['elapsed']:>6.1f}s  {r['summary'][:60]}")
    print("=" * 60)

    # 有失败则退出码非 0，方便告警
    sys.exit(1 if fail_count > 0 else 0)


if __name__ == "__main__":
    main()
