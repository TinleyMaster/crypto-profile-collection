"""读取所有帖子及其分类结果，用于人工验收"""
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

rows = conn.execute(
    """
    SELECT p.post_id, p.profile_id, p.content_text, p.image_urls,
           s.post_type, s.direction, s.symbol, s.entry_price,
           s.stop_loss, s.take_profit, s.support_level, s.resistance_level,
           s.confidence, s.already_entered, s.has_pnl_number
    FROM biz.kol_post p
    LEFT JOIN biz.kol_signal s ON p.post_id = s.post_id
    ORDER BY p.post_id
    """
).fetchall()

for r in rows:
    print(f"=== post {r['post_id']} [{r['post_type']}] conf={r['confidence']} ===")
    print(f"  direction={r['direction']}, symbol={r['symbol']}")
    print(f"  entry={r['entry_price']}, sl={r['stop_loss']}, tp={r['take_profit']}")
    print(f"  support={r['support_level']}, resistance={r['resistance_level']}")
    print(f"  already_entered={r['already_entered']}, has_pnl={r['has_pnl_number']}")
    content = (r["content_text"] or "").replace("\n", " ")
    if len(content) > 200:
        content = content[:200] + "..."
    print(f"  content: {content}")
    print()

conn.close()
