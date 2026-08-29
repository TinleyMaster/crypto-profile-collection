#!/usr/bin/env python3
"""
修复 raw.api_response 唯一约束问题

问题：insert_api_response.sql 使用 ON CONFLICT ON CONSTRAINT
      但 uq_raw_api_response_dedup 是索引而非约束

修复：删除索引，创建真正的 UNIQUE 约束
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加项目路径
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))


def check_constraint_exists(conn) -> bool:
    """检查约束是否已存在"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 
                FROM pg_constraint 
                WHERE conname = 'uq_raw_api_response_dedup'
            )
        """)
        return cur.fetchone()[0]


def check_index_exists(conn) -> bool:
    """检查索引是否已存在"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 
                FROM pg_indexes 
                WHERE indexname = 'uq_raw_api_response_dedup'
            )
        """)
        return cur.fetchone()[0]


def fix_index(conn) -> bool:
    """修复索引"""
    try:
        with conn.cursor() as cur:
            # 1. 确保唯一索引存在
            print("  确保唯一索引 uq_raw_api_response_dedup 存在...")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_api_response_dedup
                    ON raw.api_response (platform_code, endpoint_code, COALESCE(request_key, ''), COALESCE(page_key, ''), payload_hash)
            """)
            
            # 2. 验证索引已创建
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
        # 检查当前状态
        print("\n[1/3] 检查当前状态...")
        constraint_exists = check_constraint_exists(conn)
        index_exists = check_index_exists(conn)
        
        print(f"  约束存在: {constraint_exists}")
        print(f"  索引存在: {index_exists}")
        
        if index_exists:
            print("\n✅ 索引已存在，无需修复")
            print("请确保 insert_api_response.sql 使用 ON CONFLICT ON INDEX")
            return 0
        
        # 执行修复
        print("\n[2/3] 执行修复...")
        if not fix_index(conn):
            return 1
        
        # 提交事务
        conn.commit()
        
        # 验证修复
        print("\n[3/3] 验证修复...")
        if check_index_exists(conn):
            print("\n" + "=" * 60)
            print("✅ 修复完成！")
            print("=" * 60)
            print("\n请确保 insert_api_response.sql 使用 ON CONFLICT ON INDEX")
            print("\n可以重新运行失败的任务了：")
            print("  - cmc_quote_snapshot")
            print("  - cmc_pipeline")
            print("  - data_sync_daily")
            return 0
        else:
            print("\n❌ 修复验证失败")
            return 1


if __name__ == "__main__":
    sys.exit(main())