#!/usr/bin/env python3
"""触发 Zeabur 上 backfill_cycle_2020 任务并轮询进度。

通过 crypto-profile-collection 的 /api/tasks/start 端点后台启动回填，
随后轮询 /api/tasks/<id> 查看进度，直到完成或失败。

用法：python bin/trigger_backfill_2020.py
"""
from __future__ import annotations

import argparse
import sys
import time
import json
from pathlib import Path

import requests

BASE = "https://crypto-profile-collection.zeabur.app"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", action="store_true", help="只轮询已有任务（需 --task-id）")
    ap.add_argument("--task-id", default="", help="已有任务 ID（配合 --poll）")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒")
    ap.add_argument("--max-wait-min", type=int, default=240, help="最大等待分钟")
    args = ap.parse_args()

    if args.poll:
        if not args.task_id:
            print("--poll 需要 --task-id")
            return 1
        task_id = args.task_id
    else:
        print(f"[trigger] 提交回填任务到 {BASE} ...")
        r = requests.post(f"{BASE}/api/tasks/start",
                          json={"task_key": "backfill_cycle_2020"}, timeout=30)
        print(f"  状态码: {r.status_code}")
        data = r.json()
        print(f"  响应: {json.dumps(data, ensure_ascii=False)}")
        if not data.get("ok"):
            print(f"  触发失败: {data.get('error')}")
            return 1
        task_id = data.get("task_id")
        print(f"  任务 ID: {task_id}")

    # 轮询进度
    print(f"\n[poll] 轮询任务 {task_id} ...")
    start = time.time()
    last_log_len = 0
    while time.time() - start < args.max_wait_min * 60:
        try:
            r = requests.get(f"{BASE}/api/tasks/{task_id}", timeout=30)
            task = r.json().get("task", {})
            status = task.get("status")
            logs = task.get("logs") or []
            # 打印新日志
            for line in logs[last_log_len:]:
                print(f"  {line}")
            last_log_len = len(logs)
            print(f"  [status] {status} (elapsed {(time.time()-start)/60:.1f}min)")
            if status in ("done", "failed", "stopped"):
                print(f"\n[finish] 任务状态: {status}")
                if task.get("error"):
                    print(f"  错误: {task['error']}")
                stats = task.get("stats") or {}
                if stats.get("result"):
                    print(f"  结果: {json.dumps(stats['result'], ensure_ascii=False)}")
                return 0 if status == "done" else 1
        except Exception as e:  # noqa: BLE001
            print(f"  [poll] 轮询异常: {e}")
        time.sleep(args.interval)

    print(f"\n[timeout] 等待 {args.max_wait_min} 分钟未完成，任务仍在后台运行")
    print(f"  可用 --poll --task-id {task_id} 继续查看")
    return 2


if __name__ == "__main__":
    sys.exit(main())