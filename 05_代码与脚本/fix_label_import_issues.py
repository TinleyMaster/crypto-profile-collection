"""修复导入遗留问题：
1. 删除 eth 链上错误的 etherscan_label 数据（实际是 Optimism）
2. 将所有区块浏览器来源的标签置信度升级为 high
3. 打印各链最终统计
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "src"))
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from crypto_research.db.conn import get_connection
from crypto_research.config import settings

EXPLORER_SOURCES = [
    "auto_bscscan_csv",
    "bscscan_label",
    "etherscan_label",
    "basescan_label",
    "arbiscan_label",
    "polygonscan_label",
    "snowtrace_label",
    "optimism_label",
    "solscan_label",
]


def main():
    with get_connection(settings.database_url) as conn:
        cur = conn.cursor()

        # 1. 先看当前状态
        print("=" * 60)
        print("修复前状态")
        print("=" * 60)

        cur.execute("""
            SELECT chain, source, confidence, COUNT(*) as cnt
            FROM biz.onchain_exchange_wallet
            WHERE source = ANY(%s)
            GROUP BY chain, source, confidence
            ORDER BY chain, source, confidence
        """, (EXPLORER_SOURCES,))
        rows = cur.fetchall()
        print("\n[onchain_exchange_wallet] 区块浏览器来源：")
        for r in rows:
            print(f"  {r[0]:12s} {r[1]:22s} {r[2]:8s} {r[3]:>5d}")

        # 2. 删除 eth 链上错误的 etherscan_label
        print("\n" + "=" * 60)
        print("步骤 1：删除 eth 链错误的 etherscan_label 数据")
        print("=" * 60)

        cur.execute("""
            DELETE FROM biz.onchain_exchange_wallet
            WHERE chain = 'eth' AND source = 'etherscan_label'
        """)
        del_ex = cur.rowcount
        print(f"  exchange_wallet 删除 {del_ex} 条")

        cur.execute("""
            DELETE FROM biz.onchain_address_label
            WHERE chain = 'eth' AND source = 'etherscan_label'
        """)
        del_al = cur.rowcount
        print(f"  address_label 删除 {del_al} 条")

        conn.commit()
        print("  已提交")

        # 3. 升级区块浏览器来源的置信度 medium -> high
        print("\n" + "=" * 60)
        print("步骤 2：升级区块浏览器来源置信度 medium -> high")
        print("=" * 60)

        cur.execute("""
            UPDATE biz.onchain_exchange_wallet
            SET confidence = 'high',
                updated_at = CURRENT_TIMESTAMP
            WHERE confidence = 'medium'
              AND source = ANY(%s)
        """, (EXPLORER_SOURCES,))
        up_ex = cur.rowcount
        print(f"  exchange_wallet 升级 {up_ex} 条")

        cur.execute("""
            UPDATE biz.onchain_address_label
            SET confidence = 'high',
                updated_at = CURRENT_TIMESTAMP
            WHERE confidence = 'medium'
              AND source = ANY(%s)
        """, (EXPLORER_SOURCES,))
        up_al = cur.rowcount
        print(f"  address_label 升级 {up_al} 条")

        conn.commit()
        print("  已提交")

        # 4. 最终状态
        print("\n" + "=" * 60)
        print("最终状态 - onchain_exchange_wallet")
        print("=" * 60)
        cur.execute("""
            SELECT chain, confidence, COUNT(*) as cnt
            FROM biz.onchain_exchange_wallet
            GROUP BY chain, confidence
            ORDER BY chain, confidence DESC
        """)
        for r in cur.fetchall():
            print(f"  {r[0]:12s} {r[1]:8s} {r[2]:>5d}")

        print("\n" + "=" * 60)
        print("最终状态 - onchain_address_label")
        print("=" * 60)
        cur.execute("""
            SELECT chain, label_type, confidence, COUNT(*) as cnt
            FROM biz.onchain_address_label
            GROUP BY chain, label_type, confidence
            ORDER BY chain, label_type, confidence DESC
        """)
        for r in cur.fetchall():
            print(f"  {r[0]:12s} {r[1]:15s} {r[2]:8s} {r[3]:>5d}")

        print("\n全部完成。")


if __name__ == "__main__":
    main()
