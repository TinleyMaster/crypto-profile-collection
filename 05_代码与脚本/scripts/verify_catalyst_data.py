"""验证催化剂数据质量"""
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

# 总数
print("=== 总数 ===")
print(conn.execute("SELECT count(*) FROM biz.asset_catalyst").fetchone())

# 按 source_code 分布
print()
print("=== 按来源分布 ===")
rows = conn.execute(
    "SELECT source_code, count(*) FROM biz.asset_catalyst GROUP BY source_code ORDER BY 2 DESC"
).fetchall()
for r in rows:
    print(r)

# 有 pairs 的比例
print()
print("=== 有 related_pairs 的比例 ===")
print(conn.execute(
    "SELECT count(*) FROM biz.asset_catalyst WHERE related_pairs IS NOT NULL AND array_length(related_pairs, 1) > 0"
).fetchone())

# 有 asset_id 的比例
print()
print("=== 有关联 asset_id 的比例 ===")
print(conn.execute(
    "SELECT count(*) FROM biz.asset_catalyst WHERE asset_id IS NOT NULL"
).fetchone())

# 最近 5 条样本
print()
print("=== 最近 5 条样本 ===")
rows = conn.execute(
    """
    SELECT catalyst_id, source_code, title, published_at,
           related_pairs, asset_id, event_category
    FROM biz.asset_catalyst
    ORDER BY published_at DESC
    LIMIT 5
    """
).fetchall()
for r in rows:
    print(f"  [{r['catalyst_id']}] {r['title'][:60]}")
    print(f"    pairs={r['related_pairs']}, asset_id={r['asset_id']}, cat={r['event_category']}")

# 找有 pairs 的文章
print()
print("=== 有 related_pairs 的文章（最多5条） ===")
rows = conn.execute(
    """
    SELECT catalyst_id, title, related_pairs, asset_id
    FROM biz.asset_catalyst
    WHERE related_pairs IS NOT NULL AND array_length(related_pairs, 1) > 0
    ORDER BY published_at DESC
    LIMIT 5
    """
).fetchall()
for r in rows:
    print(f"  [{r['catalyst_id']}] {r['title'][:60]}")
    print(f"    pairs={r['related_pairs']}, asset_id={r['asset_id']}")

if not rows:
    print("  (无)")

conn.close()
