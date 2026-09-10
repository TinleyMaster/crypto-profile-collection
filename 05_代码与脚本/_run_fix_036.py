"""执行 fix_036 迁移：为 onchain_transfer_log 添加标签数组列并回填历史数据"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)
sql_path = Path(__file__).parent / "scripts" / "migrations" / "fix_036_add_transfer_label_arrays.sql"

sql = sql_path.read_text(encoding="utf-8")
print(f"执行迁移: {sql_path.name}")
print(f"SQL 长度: {len(sql)} 字符")

with get_connection(settings.database_url) as conn:
    print("执行 DDL + 回填...")
    try:
        conn.execute(sql)
        conn.commit()
        print("✅ 迁移成功！")
    except Exception as e:
        conn.rollback()
        print(f"❌ 迁移失败: {e}")
        raise

    # 验证
    print("\n验证：")
    cur = conn.cursor()

    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'biz'
          AND table_name = 'onchain_transfer_log'
          AND column_name LIKE '%_labels'
        ORDER BY column_name
    """)
    print("  新列：")
    for r in cur.fetchall():
        print(f"    {r[0]:20s} {r[1]}")

    cur.execute("""
        SELECT COUNT(*) as total,
               COUNT(from_labels) as with_from_labels,
               COUNT(to_labels) as with_to_labels
        FROM biz.onchain_transfer_log
    """)
    row = cur.fetchone()
    print(f"\n  数据回填：")
    print(f"    总记录数: {row[0]}")
    print(f"    有 from_labels: {row[1]}")
    print(f"    有 to_labels: {row[2]}")
