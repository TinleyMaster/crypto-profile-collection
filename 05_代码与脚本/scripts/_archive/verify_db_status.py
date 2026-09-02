"""验证数据库当前状态：列、约束、数据分布"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto",
)

import psycopg
from psycopg.rows import dict_row

conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10)

# 1. 检查列是否存在
print("=== 列检查 ===")
rows = conn.execute(
    """
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'biz' AND table_name = 'kol_signal'
      AND column_name IN ('support_level', 'resistance_level', 'post_type')
    ORDER BY ordinal_position
    """
).fetchall()
for r in rows:
    print(r)

# 2. 检查约束
print()
print("=== 约束检查 ===")
rows = conn.execute(
    """
    SELECT conname, pg_get_constraintdef(oid) as consrc
    FROM pg_constraint
    WHERE conname = 'chk_kol_signal_post_type'
    """
).fetchall()
for r in rows:
    print(r)

# 3. 当前分布
print()
print("=== 当前 post_type 分布 ===")
rows = conn.execute(
    "SELECT post_type, count(*) FROM biz.kol_signal GROUP BY post_type ORDER BY 2 DESC"
).fetchall()
for r in rows:
    print(r)

# 4. 帖子总数
print()
print("=== kol_post 总数 ===")
print(conn.execute("SELECT count(*) FROM biz.kol_post").fetchone())

# 5. 信号总数
print()
print("=== kol_signal 总数 ===")
print(conn.execute("SELECT count(*) FROM biz.kol_signal").fetchone())

# 6. 检查 post 33 和 35 的当前字段
print()
print("=== post 33 / 35 当前字段 ===")
rows = conn.execute(
    """
    SELECT s.post_id, s.post_type, s.entry_price, s.stop_loss, s.take_profit,
           s.support_level, s.resistance_level, s.confidence
    FROM biz.kol_signal s
    WHERE s.post_id IN (33, 35)
    ORDER BY s.post_id
    """
).fetchall()
for r in rows:
    print(r)

conn.close()
print()
print("Done.")
