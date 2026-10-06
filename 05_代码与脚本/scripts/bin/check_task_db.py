#!/usr/bin/env python3
"""查看任务在数据库中的状态和日志。用法：python bin/check_task_db.py <task_id>"""
import os
import sys
from pathlib import Path

import psycopg

env_path = Path(__file__).resolve().parent.parent / ".env"
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

task_id = sys.argv[1] if len(sys.argv) > 1 else "3b8f22d6df3e"

conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=20)
with conn.cursor() as cur:
    # 表结构
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='sys' AND table_name='task_log' ORDER BY ordinal_position"
    )
    cols = [r[0] for r in cur.fetchall()]
    print("task_log columns:", cols)

    cur.execute("SELECT task_id, status, name, cmd FROM sys.task WHERE task_id=%s", (task_id,))
    for r in cur.fetchall():
        print("TASK:", r[0], r[1], r[2])
        print("CMD:", r[3])

    # 用第一个非 id/ts 文本列作为日志列
    log_col = next((c for c in cols if c not in ("task_id", "ts", "id", "created_at", "log_id", "line_no")), "content")
    try:
        cur.execute(
            f"SELECT {log_col} FROM sys.task_log WHERE task_id=%s ORDER BY line_no ASC",
            (task_id,),
        )
        logs = cur.fetchall()
        print(f"LOG rows: {len(logs)}")
        for l in logs[-30:]:
            print(" ", l[0])
    except Exception as e:  # noqa: BLE001
        print("log query err:", e)
conn.close()