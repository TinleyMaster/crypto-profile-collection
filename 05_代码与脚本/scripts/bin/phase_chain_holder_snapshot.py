"""
Phase 1: 链上持仓快照采集。
从 Etherscan/BSCScan API 拉取代币 Top 持有者，计算持仓集中度。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone

import psycopg
import psycopg.rows

# 确保能找到 src 模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from crypto_research.config import get_settings
from crypto_research.clients.etherscan_client import EtherscanClient, get_client


def get_asset_contracts(conn, asset_id: int | None = None) -> list[dict]:
    """获取需要采集持仓数据的资产及其合约地址。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if asset_id:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       m.chain, m.contract_address
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.asset_id = %s AND a.status = 'active'
                ORDER BY a.asset_id
            """, (asset_id,))
        else:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       m.chain, m.contract_address
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.status = 'active'
                ORDER BY a.asset_id
            """)
        return [dict(r) for r in cur.fetchall()]


def get_exchange_addresses(conn, chain: str) -> set[str]:
    """获取指定链的交易所钱包地址集合。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT LOWER(address) AS address
            FROM biz.onchain_exchange_wallet
            WHERE chain = %s
        """, (chain,))
        return {r["address"] for r in cur.fetchall()}


def get_previous_snapshot(conn, asset_id: int, chain: str, days_ago: int) -> dict | None:
    """获取 N 天前的持仓快照，用于计算变化趋势。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT * FROM biz.onchain_holder_snapshot
            WHERE asset_id = %s AND chain = %s
              AND snapshot_date <= CURRENT_DATE - %s::int
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, (asset_id, chain, days_ago))
        return dict(cur.fetchone()) if cur.rowcount else None


def save_snapshot(conn, snapshot: dict) -> int:
    """保存持仓快照到数据库。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            INSERT INTO biz.onchain_holder_snapshot (
                asset_id, chain, contract_address, snapshot_date,
                top10_concentration, top50_concentration, top100_concentration,
                total_holders, holder_change_7d, holder_change_30d,
                whale_balance_change_7d_pct, whale_balance_change_30d_pct,
                exchange_wallet_pct, vc_wallet_pct, smart_money_pct,
                retail_pct, contract_pct
            ) VALUES (
                %(asset_id)s, %(chain)s, %(contract_address)s, %(snapshot_date)s,
                %(top10_concentration)s, %(top50_concentration)s, %(top100_concentration)s,
                %(total_holders)s, %(holder_change_7d)s, %(holder_change_30d)s,
                %(whale_balance_change_7d_pct)s, %(whale_balance_change_30d_pct)s,
                %(exchange_wallet_pct)s, %(vc_wallet_pct)s, %(smart_money_pct)s,
                %(retail_pct)s, %(contract_pct)s
            )
            ON CONFLICT DO NOTHING
            RETURNING snapshot_id
        """, snapshot)
        row = cur.fetchone()
    conn.commit()
    return row["snapshot_id"] if row else -1


def collect_holder_snapshot(
    conn,
    client: EtherscanClient,
    asset: dict,
    exchange_addresses: set[str],
    dry_run: bool = False,
) -> dict | None:
    """采集单个资产的持仓快照。"""
    asset_id = asset["asset_id"]
    symbol = asset["canonical_symbol"]
    chain = asset["chain"]
    contract_address = asset["contract_address"]

    if not contract_address:
        return None

    # 获取 Top 100 持有者
    all_holders: list[dict] = []
    for page in range(1, 6):  # 每页 100，最多 5 页 = 500 个持有者
        holders = client.get_token_holders(contract_address, page=page, offset=100)
        if not holders:
            break
        all_holders.extend(holders)
        if len(holders) < 100:
            break

    if not all_holders:
        print(f"  [{symbol}] {chain}: 无持有者数据")
        return None

    # 计算持仓占比（需要获取总供应量）
    total_supply = 0.0
    for h in all_holders:
        total_supply += float(h.get("balance", 0)) / float(h.get("share", "1") or "1")

    # 如果没有 share 字段，用 balance 之和作为总量
    # Etherscan 的 tokenholderlist 不返回 share，需要另外获取 totalSupply
    # 简化处理：用所有持有者的 balance 之和作为近似总量
    if total_supply == 0:
        for h in all_holders:
            total_supply += float(h.get("balance", 0))

    if total_supply == 0:
        return None

    total_holders = len(all_holders)

    # 计算集中度
    top10_balance = sum(float(h.get("balance", 0)) for h in all_holders[:10])
    top50_balance = sum(float(h.get("balance", 0)) for h in all_holders[:50])
    top100_balance = sum(float(h.get("balance", 0)) for h in all_holders[:100])

    top10_pct = round(top10_balance / total_supply * 100, 2)
    top50_pct = round(top50_balance / total_supply * 100, 2)
    top100_pct = round(top100_balance / total_supply * 100, 2)

    # 地址类型分布（基于交易所钱包标签）
    exchange_balance = 0.0
    for h in all_holders:
        addr = h.get("address", "").lower()
        if addr in exchange_addresses:
            exchange_balance += float(h.get("balance", 0))
    exchange_pct = round(exchange_balance / total_supply * 100, 2) if total_supply > 0 else 0

    # 计算趋势（与历史快照对比）
    prev_7d = get_previous_snapshot(conn, asset_id, chain, 7)
    prev_30d = get_previous_snapshot(conn, asset_id, chain, 30)

    holder_change_7d = None
    holder_change_30d = None
    whale_change_7d = None
    whale_change_30d = None

    if prev_7d and prev_7d.get("total_holders"):
        holder_change_7d = total_holders - prev_7d["total_holders"]
        if prev_7d.get("top10_concentration"):
            whale_change_7d = round(top10_pct - prev_7d["top10_concentration"], 2)

    if prev_30d and prev_30d.get("total_holders"):
        holder_change_30d = total_holders - prev_30d["total_holders"]
        if prev_30d.get("top10_concentration"):
            whale_change_30d = round(top10_pct - prev_30d["top10_concentration"], 2)

    snapshot = {
        "asset_id": asset_id,
        "chain": chain,
        "contract_address": contract_address,
        "snapshot_date": date.today().isoformat(),
        "top10_concentration": top10_pct,
        "top50_concentration": top50_pct,
        "top100_concentration": top100_pct,
        "total_holders": total_holders,
        "holder_change_7d": holder_change_7d,
        "holder_change_30d": holder_change_30d,
        "whale_balance_change_7d_pct": whale_change_7d,
        "whale_balance_change_30d_pct": whale_change_30d,
        "exchange_wallet_pct": exchange_pct,
        "vc_wallet_pct": None,
        "smart_money_pct": None,
        "retail_pct": None,
        "contract_pct": None,
    }

    if dry_run:
        print(f"  [{symbol}] {chain}: Top10={top10_pct}% Top50={top50_pct}% Holders={total_holders} (dry-run)")
    else:
        sid = save_snapshot(conn, snapshot)
        print(f"  [{symbol}] {chain}: Top10={top10_pct}% Top50={top50_pct}% Holders={total_holders} -> snapshot_id={sid}")

    return snapshot


def main():
    parser = argparse.ArgumentParser(description="链上持仓快照采集")
    parser.add_argument("--asset-id", type=int, default=None, help="指定资产 ID（可选，不指定则全量）")
    parser.add_argument("--chain", type=str, default=None, help="指定链（eth/bsc，不指定则全部）")
    parser.add_argument("--limit", type=int, default=50, help="最大处理资产数")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写入数据库")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with psycopg.connect(settings.database_url) as conn:
        assets = get_asset_contracts(conn, args.asset_id)

        if args.chain:
            assets = [a for a in assets if a["chain"] == args.chain]
        if args.limit > 0:
            assets = assets[:args.limit]

        print(f"共 {len(assets)} 个资产待采集\n")

        # 按链分组，准备客户端
        chain_clients = {}
        chain_exchanges = {}

        success = 0
        skip = 0
        t0 = time.time()

        for i, asset in enumerate(assets, 1):
            chain = asset["chain"]

            # 缓存客户端
            if chain not in chain_clients:
                client = get_client(chain)
                if not client:
                    print(f"  [{i}/{len(assets)}] 跳过 {chain}: 无 API Key")
                    skip += 1
                    continue
                chain_clients[chain] = client
                chain_exchanges[chain] = get_exchange_addresses(conn, chain)

            client = chain_clients[chain]
            exchanges = chain_exchanges[chain]

            result = collect_holder_snapshot(
                conn, client, asset, exchanges,
                dry_run=args.dry_run,
            )
            if result:
                success += 1
            else:
                skip += 1

        elapsed = time.time() - t0
        print(f"\n完成: {success} 成功, {skip} 跳过, 耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()