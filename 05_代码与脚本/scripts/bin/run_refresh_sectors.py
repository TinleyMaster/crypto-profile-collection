"""执行多来源赛道标签全量刷新（调用 SQL 版脚本）。

用于流水线末尾和定时任务，确保所有资产赛道标签最新。
SQL 版比 Python 版快得多（秒级 vs 分钟级），且规则与 sector.py 统一维护。

用法:
    python run_refresh_sectors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


SQL_PATH = SCRIPT_DIR.parent / "sql" / "biz" / "refresh_sectors_multi_source.sql"


def main() -> int:
    settings = get_settings()
    sql = SQL_PATH.read_text(encoding="utf-8")

    print(f"执行赛道全量刷新: {SQL_PATH.name}")
    print("=" * 60, flush=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            # 跳到最后一个结果集（统计结果）
            while cur.nextset():
                pass
            rows = cur.fetchall()

    print("\n=== 主赛道分布 ===")
    for sector, cnt, pct in rows:
        bar = "█" * int(float(pct) / 2)
        print(f"  {sector:<12} {cnt:>6d} ({pct:>5}%) {bar}")

    total = sum(int(r[1]) for r in rows)
    other_cnt = next((int(r[1]) for r in rows if r[0] == "other"), 0)
    print(f"\n总计: {total} 个资产，other 占 {other_cnt / total * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
