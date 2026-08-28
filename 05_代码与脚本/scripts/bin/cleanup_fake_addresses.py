# -*- coding: utf-8 -*-
"""
清理 CEX 地址库中的假占位地址（工单：大盘_基础_CEX地址自动收集）。

检测逻辑（来自审计脚本 audit_cex_wallets_2026-08-28.py）：
  1. 主检测器：0x[0-9a-f]*?(([0-9a-f]{2})\2{3,})（2-hex 段连续重复 >=4 次）
  2. 交替重复：Binance BSC 0xBd7D7B7D7B...（7D7B 交替）

操作：
  1. 备份假地址到 JSON 文件（审计追踪）
  2. 从 DB 删除假地址
  3. 输出清理报告

用法：
  python cleanup_fake_addresses.py              # 预览不删除
  python cleanup_fake_addresses.py --apply      # 执行删除
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection

# 假地址检测器
# 主检测器：2-hex 段连续重复 >=4 次（真实随机地址概率趋零）
PLACEHOLDER_REGEX = re.compile(r'0x[0-9a-f]*?(([0-9a-f]{2})\2{3,})')

# Binance BSC 交替重复模式（7D7B 交替）
BINANCE_BSC_FAKE = "0xBd7D7B7D7B7D7B7D7B7D7B7D7B7D7B7D7B7D7B"


def detect_fake_addresses(conn) -> list[dict]:
    """检测所有假占位地址。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT wallet_id, address, exchange_name, chain, confidence, source, added_at
            FROM biz.onchain_exchange_wallet
            ORDER BY exchange_name, chain, address
        """)
        all_rows = cur.fetchall()

    fake_addresses = []
    for row in all_rows:
        addr_lower = row["address"].lower()
        is_fake = False
        reason = ""

        # 主检测器：2-hex 段连续重复 >=4 次
        if PLACEHOLDER_REGEX.search(addr_lower):
            is_fake = True
            reason = "2-hex repeat >=4"

        # 交替重复：Binance BSC
        elif addr_lower == BINANCE_BSC_FAKE.lower():
            is_fake = True
            reason = "alternating repeat 7D7B"

        if is_fake:
            fake_addresses.append({
                **row,
                "added_at": row["added_at"].isoformat() if row["added_at"] else None,
                "detection_reason": reason,
            })

    return fake_addresses


def delete_fake_addresses(conn, fake_addresses: list[dict], dry_run: bool = True) -> int:
    """删除假地址。"""
    if not fake_addresses:
        return 0

    deleted = 0
    with conn.cursor() as cur:
        for fake in fake_addresses:
            if dry_run:
                deleted += 1
                continue
            try:
                cur.execute("""
                    DELETE FROM biz.onchain_exchange_wallet
                    WHERE wallet_id = %s
                """, (fake["wallet_id"],))
                if cur.rowcount:
                    deleted += 1
            except Exception as e:
                print(f"  [WARN] 删除失败 {fake['exchange_name']} {fake['chain']}: {e}")

    if not dry_run:
        conn.commit()

    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 CEX 地址库假占位地址")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认预览）")
    parser.add_argument("--output", type=str, help="备份文件路径（默认自动生成）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with get_connection(settings.database_url) as conn:
        print("=" * 60)
        print("CEX 地址库假占位地址清理")
        print("=" * 60)

        # 检测假地址
        print("\n[1/3] 检测假占位地址...")
        fake_addresses = detect_fake_addresses(conn)
        print(f"  检测到 {len(fake_addresses)} 条假地址")

        if not fake_addresses:
            print("\n✅ 无假地址，无需清理")
            return 0

        # 显示假地址列表
        print("\n假地址列表：")
        print(f"  {'交易所':12s} {'链':10s} {'置信度':8s} {'来源':16s} {'检测原因':20s} {'地址'}")
        print(f"  {'-'*12} {'-'*10} {'-'*8} {'-'*16} {'-'*20} {'-'*40}")
        for fake in fake_addresses:
            print(f"  {fake['exchange_name']:12s} {fake['chain']:10s} {fake['confidence']:8s} "
                  f"{fake['source']:16s} {fake['detection_reason']:20s} {fake['address']}")

        # 备份到 JSON
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = args.output or str(SCRIPT_DIR / f"fake_addresses_backup_{timestamp}.json")
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_fake": len(fake_addresses),
                "fake_addresses": fake_addresses,
            }, f, indent=2, ensure_ascii=False)
        print(f"\n[2/3] 备份到: {backup_file}")

        # 删除
        print(f"\n[3/3] {'[DRY-RUN] ' if not args.apply else ''}删除假地址...")
        deleted = delete_fake_addresses(conn, fake_addresses, dry_run=not args.apply)
        print(f"  {'[DRY-RUN] 将' if not args.apply else '已'}删除 {deleted} 条假地址")

        # 汇总
        print("\n" + "=" * 60)
        print("清理汇总")
        print("=" * 60)
        print(f"  假地址总数: {len(fake_addresses)}")
        print(f"  已删除: {deleted}")
        print(f"  备份文件: {backup_file}")
        if not args.apply:
            print("\n⚠️  预览模式，未实际删除。使用 --apply 执行删除。")
        else:
            print("\n✅ 清理完成")

    return 0


if __name__ == "__main__":
    sys.exit(main())
