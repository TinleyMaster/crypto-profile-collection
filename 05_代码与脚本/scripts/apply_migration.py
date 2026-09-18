#!/usr/bin/env python3
"""通用 SQL 迁移执行器：读取 DATABASE_URL 执行指定迁移文件（幂等，可重复跑）。

用法：
    python apply_migration.py fix_043_scan_data_tables.sql   # 相对 migrations/ 目录
    python apply_migration.py migrations/xxx.sql             # 或带路径
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python apply_migration.py <迁移文件名或路径>")
        return 1

    arg = sys.argv[1]
    candidate = Path(arg)
    if not candidate.is_absolute():
        candidate = SCRIPT_DIR / candidate
    if not candidate.exists():
        candidate = SCRIPT_DIR / "migrations" / arg
    if not candidate.exists():
        print(f"迁移文件不存在: {arg}")
        return 1

    settings = get_settings(require_database=True)
    sql = candidate.read_text(encoding="utf-8")
    print(f"执行迁移: {candidate.name}（{len(sql)} 字符）")

    with psycopg.connect(
        settings.database_url,
        connect_timeout=30,
        options="-c lock_timeout=30000",
    ) as conn:
        conn.execute(sql)
        conn.commit()
    print("执行成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
