import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "scripts" / "src"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

settings = get_settings(require_database=True)

with get_connection(settings.database_url) as conn:
    cur = conn.cursor()

    # 1. 各链交易所地址统计
    cur.execute("""
        SELECT chain, confidence, COUNT(*) as cnt
        FROM biz.onchain_exchange_wallet
        GROUP BY chain, confidence
        ORDER BY chain, confidence DESC
    """)
    print('=== onchain_exchange_wallet 各链分布 ===')
    for r in cur.fetchall():
        print(f'  {r[0]:12s} {r[1]:8s} {r[2]:>5d}')

    # 2. 各链来源统计
    cur.execute("""
        SELECT chain, source, confidence, COUNT(*) as cnt
        FROM biz.onchain_exchange_wallet
        WHERE source LIKE '%scan%' OR source LIKE '%label' OR source = 'auto_bscscan_csv'
        GROUP BY chain, source, confidence
        ORDER BY chain, source
    """)
    print('\n=== 区块浏览器来源明细 ===')
    for r in cur.fetchall():
        print(f'  {r[0]:12s} {r[1]:22s} {r[2]:8s} {r[3]:>5d}')

    # 3. onchain_address_label 各链统计
    cur.execute("""
        SELECT chain, label_type, confidence, COUNT(*) as cnt
        FROM biz.onchain_address_label
        GROUP BY chain, label_type, confidence
        ORDER BY chain, label_type, confidence DESC
    """)
    print('\n=== onchain_address_label 各链分布 ===')
    for r in cur.fetchall():
        print(f'  {r[0]:12s} {r[1]:15s} {r[2]:8s} {r[3]:>5d}')
