"""执行官网 primary 裁决（全量重算）。

用于流水线末尾和定时任务，确保每个资产只有一个主官网。
裁决规则：来源可信度（CMC > Binance > DL > CG > DexScreener）+ 路径深度 + URL 长度。

用法:
    python run_refresh_primary_website.py
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


SQL_PATH = SCRIPT_DIR.parent / "sql" / "biz" / "refresh_primary_website.sql"


def main() -> int:
    settings = get_settings()
    sql = SQL_PATH.read_text(encoding="utf-8")

    print(f"执行官网 primary 裁决: {SQL_PATH.name}")
    print("=" * 60, flush=True)

    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            # 跳到最后一个结果集（统计结果）
            while cur.nextset():
                pass
            row = cur.fetchone()

    with_primary, no_website, multi_primary, pct = row
    print(f"\n=== 裁决结果 ===")
    print(f"  有主官网的资产:   {with_primary:>6d} ({pct}%)")
    print(f"  无官网的资产:     {no_website:>6d}")
    print(f"  多主官网（异常）: {multi_primary:>6d}")

    if multi_primary > 0:
        print(f"\n  ⚠️  仍有 {multi_primary} 个资产存在多主官网，需排查")
        return 1

    print("\n裁决完成 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
