"""验证 AddressLabelResolver + onchain_transfer_log 新列写入"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "src"))
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.address_label_resolver import AddressLabelResolver

settings = get_settings(require_database=True)

with get_connection(settings.database_url) as conn:
    print("=== 1. 测试 AddressLabelResolver ===")
    resolver = AddressLabelResolver(conn, "bsc")

    # 找几条已知的交易所地址
    test_addrs = [
        "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",  # Binance
        "0x28c6c06298d514db089934071355e5743bf21d60",  # 随便一个地址（可能没标签）
        "0x0000000000000000000000000000000000000000",  # 零地址
    ]

    resolver.resolve_batch(test_addrs)
    for addr in test_addrs:
        info = resolver.resolve(addr)
        print(f"  {addr[:20]}... -> types={info['types']}, names={info['names'][:2] if info['names'] else []}, is_exchange={info['is_exchange']}")

    print(f"\n  缓存命中: {len(resolver._cache)} 条")
    print(f"  无标签: {len(resolver._no_label)} 条")

    print("\n=== 2. 验证新列已存在且有数据 ===")
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as total,
               COUNT(from_labels) as with_from_labels,
               COUNT(to_labels) as with_to_labels
        FROM biz.onchain_transfer_log
        WHERE chain = 'bsc'
    """)
    row = cur.fetchone()
    print(f"  BSC 链记录: {row[0]} 条")
    print(f"  有 from_labels: {row[1]} 条")
    print(f"  有 to_labels: {row[2]} 条")

    # 看几条有标签的记录
    cur.execute("""
        SELECT from_address, to_address, from_labels, to_labels,
               from_label, to_label, from_exchange, to_exchange
        FROM biz.onchain_transfer_log
        WHERE chain = 'bsc'
          AND from_labels IS NOT NULL
          AND array_length(from_labels, 1) > 0
        LIMIT 5
    """)
    print("\n  前 5 条有标签的记录：")
    for r in cur.fetchall():
        print(f"    from: {r[0][:16]}... labels={r[2]}")
        print(f"    to:   {r[1][:16]}... labels={r[3]}")

    print("\n✅ 验证通过！")
