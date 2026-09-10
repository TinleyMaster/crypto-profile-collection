import sys
sys.path.insert(0, 'scripts/src')
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

s = get_settings(require_database=True)
with get_connection(s.database_url) as conn:
    cur = conn.cursor()
    cur.execute("DELETE FROM biz.onchain_address_label WHERE label_type = 'other'")
    deleted = cur.rowcount
    conn.commit()
    cur.execute("SELECT count(*) FROM biz.onchain_address_label")
    total = cur.fetchone()[0]
    print(f"已删除 other 类型标签: {deleted:,} 条")
    print(f"剩余总标签数: {total:,} 条")
