"""执行 SQL 迁移文件"""
import psycopg

DB_URL = "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto"

sql_path = __import__('pathlib').Path(__file__).resolve().parent / "migrations" / "fix_003_005_008.sql"
sql = sql_path.read_text(encoding="utf-8")

print(f"执行 SQL 迁移: {sql_path.name}")
print(f"SQL 长度: {len(sql)} 字符")
print("连接数据库...")

conn = psycopg.connect(DB_URL, connect_timeout=30)
print("已连接，执行中...")

try:
    conn.execute(sql)
    conn.commit()
    print("执行成功！")
except Exception as e:
    conn.rollback()
    print(f"执行失败: {e}")
    raise
finally:
    conn.close()
    print("连接已关闭")