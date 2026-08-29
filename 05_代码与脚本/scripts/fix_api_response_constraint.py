#!/usr/bin/env python3
"""
修复 raw.api_response 唯一约束问题

问题：insert_api_response.sql 使用 ON CONFLICT ON INDEX（非法 PG 语法）
      且 ON CONFLICT ON CONSTRAINT 也失败（索引非约束）

修复：insert_api_response.sql 改用 ON CONFLICT (expr) 语法
      PostgreSQL 支持列表达式匹配唯一索引，无需新建约束或函数
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加项目路径
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def check_index_exists(conn) -> bool:
    """检查唯一索引是否已存在"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE indexname = 'uq_raw_api_response_dedup'
            )
        """)
        return cur.fetchone()[0]


def ensure_index(conn) -> bool:
    """确保唯一索引存在（幂等）"""
    try:
        with conn.cursor() as cur:
            print("  确保唯一索引 uq_raw_api_response_dedup 存在...")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_api_response_dedup
                    ON raw.api_response (platform_code, endpoint_code, COALESCE(request_key, ''), COALESCE(page_key, ''), payload_hash)
            """)
            if check_index_exists(conn):
                print("  ✅ 索引创建/验证成功")
                return True
            else:
                print("  ❌ 索引创建失败")
                return False
    except Exception as e:
        print(f"  ❌ 修复失败: {e}")
        conn.rollback()
        return False


def main() -> int:
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection

    print("=" * 60)
    print("修复 raw.api_response 唯一约束问题")
    print("=" * 60)

    settings = get_settings(require_database=True)

    if not settings.database_url:
        print("❌ DATABASE_URL 未配置")
        return 1

    print(f"\n数据库: {settings.database_url.split('@')[-1] if '@' in settings.database_url else '***'}")

    with get_connection(settings.database_url) as conn:
        print("\n[1/2] 检查当前状态...")
        index_exists = check_index_exists(conn)
        print(f"  索引存在: {index_exists}")

        if index_exists:
            print("\n✅ 索引已存在，无需修复")
            print("请确保 insert_api_response.sql 使用 ON CONFLICT (expr) 语法")
            return 0

        print("\n[2/2] 执行修复...")
        if not ensure_index(conn):
            return 1

        conn.commit()

        print("\n" + "=" * 60)
        print("✅ 修复完成！")
        print("=" * 60)
        print("\n请确保 insert_api_response.sql 使用 ON CONFLICT (expr) 语法")
        print("\n可以重新运行失败的任务了")
        return 0


if __name__ == "__main__":
    sys.exit(main())
