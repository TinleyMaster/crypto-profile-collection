"""一次性修复 + 补充导入：
1. 删除 eth 链上错误的 etherscan_label 数据（实际是 Optimism）
2. 升级 BSC/Base/Arbitrum/Polygon 等链的 medium -> high
3. 导入 Optimism 链（用之前搞错的那个 CSV）
4. 导入 Solana 剩下的 3 个文件
5. 打印最终统计
"""
import sys
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts" / "src"))
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# 复用 import_bscscan_labels 里的解析和导入逻辑
sys.path.insert(0, str(Path(__file__).parent / "scripts" / "bin"))
from import_bscscan_labels import (
    parse_label_csv, diff_against_db, apply_import,
)

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

# 需要补充导入的文件
IMPORT_JOBS = [
    # Optimism（之前误导入为 eth 链，先删再正确导入）
    (r"C:\Users\SuperTing\Downloads\etherscan-2026-09-09 (1).csv", "optimism", "optimism_label", "high"),
    # Solana 补充文件
    (r"C:\Users\SuperTing\Downloads\solscan-1-2026-09-09.csv", "solana", "solscan_label", "high"),
    (r"C:\Users\SuperTing\Downloads\solscan-2-2026-09-09.csv", "solana", "solscan_label", "high"),
    (r"C:\Users\SuperTing\Downloads\solscan-3-2026-09-09.csv", "solana", "solscan_label", "high"),
]


def print_status(conn, title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)

    cur = conn.cursor()
    cur.execute("""
        SELECT chain, confidence, COUNT(*) as cnt
        FROM biz.onchain_exchange_wallet
        GROUP BY chain, confidence
        ORDER BY chain, confidence DESC
    """)
    print("\n[onchain_exchange_wallet] 各链置信度分布：")
    for r in cur.fetchall():
        print(f"  {r[0]:12s} {r[1]:8s} {r[2]:>5d}")

    cur.execute("""
        SELECT chain, label_type, confidence, COUNT(*) as cnt
        FROM biz.onchain_address_label
        GROUP BY chain, label_type, confidence
        ORDER BY chain, label_type, confidence DESC
    """)
    print("\n[onchain_address_label] 各链分布：")
    for r in cur.fetchall():
        print(f"  {r[0]:12s} {r[1]:15s} {r[2]:8s} {r[3]:>5d}")


def main():
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        # 1. 当前状态
        print_status(conn, "当前状态")

        # 2. 升级 medium -> high
        print("\n" + "=" * 70)
        print("步骤 1：升级区块浏览器来源置信度 medium -> high")
        print("=" * 70)
        cur = conn.cursor()

        cur.execute("""
            UPDATE biz.onchain_exchange_wallet
            SET confidence = 'high'
            WHERE confidence = 'medium'
              AND source = ANY(%s)
        """, (EXPLORER_SOURCES,))
        print(f"  exchange_wallet 升级 {cur.rowcount} 条")

        cur.execute("""
            UPDATE biz.onchain_address_label
            SET confidence = 'high',
                updated_at = CURRENT_TIMESTAMP
            WHERE confidence = 'medium'
              AND source = ANY(%s)
        """, (EXPLORER_SOURCES,))
        print(f"  address_label 升级 {cur.rowcount} 条")
        conn.commit()
        print("  已提交")

        # 3. 补充导入
        print("\n" + "=" * 70)
        print("步骤 2：补充导入剩余链")
        print("=" * 70)

        for csv_path, chain, source, conf in IMPORT_JOBS:
            p = Path(csv_path)
            if not p.exists():
                print(f"\n  [跳过] 文件不存在: {csv_path}")
                continue

            print(f"\n  --- {chain} | {p.name} ---")
            records = parse_label_csv(p, chain)
            print(f"    解析: {len(records)} 条")

            diff = diff_against_db(conn, records, chain)
            print(f"    交易所新增: {diff['old_table_new']} 条")
            print(f"    标签新增: {diff['new_table_new']} 条")

            if diff["old_table_new"] > 0 or diff["new_table_new"] > 0:
                result = apply_import(conn, records, chain, source, conf)
                conn.commit()
                print(f"    导入完成: exchange +{result['inserted_exchange']}, label +{result['inserted_label']}")
            else:
                print(f"    无新增，跳过")

        # 5. 最终状态
        print_status(conn, "最终状态")
        print("\n全部完成。")


if __name__ == "__main__":
    main()
