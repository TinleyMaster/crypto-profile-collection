"""
Phase 1: 大额转账监控。
从 Etherscan/BSCScan API 拉取代币大额转账，标记转入交易所的潜在砸盘信号。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import psycopg
import psycopg.rows

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from crypto_research.config import get_settings
from crypto_research.clients.etherscan_client import EtherscanClient, get_client


# 大额转账阈值（美元）
LARGE_TRANSFER_THRESHOLD_USD = 100_000

# 热门代币的参考价格（美元），用于粗略估算
# 实际使用时可通过 CoinGecko API 获取实时价格
FALLBACK_PRICES = {
    "eth": 2000.0,
    "weth": 2000.0,
    "usdt": 1.0,
    "usdc": 1.0,
    "dai": 1.0,
    "busd": 1.0,
    "wbnb": 300.0,
    "bnb": 300.0,
    "cake": 2.0,
    "uni": 5.0,
    "link": 15.0,
    "aave": 100.0,
    "matic": 0.5,
    "pol": 0.5,
}


def get_asset_contracts(conn, asset_id: int | None = None) -> list[dict]:
    """获取需要监控转账的资产及其合约地址。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if asset_id:
            cur.execute("""
                SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                       m.chain, m.contract_address
                FROM core.asset a
                INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
                WHERE a.asset_id = %s AND a.status = 'active'
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


def get_exchange_map(conn, chain: str) -> dict[str, str]:
    """获取指定链的交易所钱包地址 -> 交易所名称映射。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT LOWER(address) AS address, exchange_name
            FROM biz.onchain_exchange_wallet
            WHERE chain = %s
        """, (chain,))
        return {r["address"]: r["exchange_name"] for r in cur.fetchall()}


def get_last_block(conn, chain: str, contract_address: str) -> int:
    """获取上次扫描到的区块号。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT MAX(block_number) AS last_block
            FROM biz.onchain_transfer_log
            WHERE chain = %s AND contract_address = %s
        """, (chain, contract_address))
        row = cur.fetchone()
        return row["last_block"] or 0 if row else 0


def save_transfers(conn, transfers: list[dict]) -> int:
    """批量保存转账记录。"""
    written = 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for t in transfers:
            try:
                cur.execute("""
                    INSERT INTO biz.onchain_transfer_log (
                        asset_id, chain, contract_address, tx_hash,
                        from_address, to_address, value, value_usd,
                        from_label, to_label, from_exchange, to_exchange,
                        block_number, block_timestamp, is_to_exchange
                    ) VALUES (
                        %(asset_id)s, %(chain)s, %(contract_address)s, %(tx_hash)s,
                        %(from_address)s, %(to_address)s, %(value)s, %(value_usd)s,
                        %(from_label)s, %(to_label)s, %(from_exchange)s, %(to_exchange)s,
                        %(block_number)s, %(block_timestamp)s, %(is_to_exchange)s
                    )
                    ON CONFLICT (chain, tx_hash, contract_address, from_address, to_address) DO NOTHING
                """, t)
                if cur.rowcount:
                    written += 1
            except Exception:
                continue
    conn.commit()
    return written


def collect_transfers(
    conn,
    client: EtherscanClient,
    asset: dict,
    exchange_map: dict[str, str],
    dry_run: bool = False,
    alarm_only: bool = False,
) -> dict:
    """采集单个资产的大额转账。
    alarm_only=True: 只存储转入交易所的告警，不存普通大额转账。"""
    asset_id = asset["asset_id"]
    symbol = asset["canonical_symbol"]
    chain = asset["chain"]
    contract_address = asset["contract_address"]

    if not contract_address:
        return {"asset_id": asset_id, "symbol": symbol, "processed": 0, "written": 0}

    # 获取上次扫描到的区块号，从该区块之后开始扫描
    last_block = get_last_block(conn, chain, contract_address)

    # 获取代币价格（从 CoinGecko 或使用 fallback）
    price_usd = FALLBACK_PRICES.get(symbol.lower(), 0.0)

    all_transfers = []
    total_processed = 0

    # 分页拉取转账记录
    for page in range(1, 11):  # 最多 10 页 = 1000 条转账
        transfers = client.get_token_transfers(
            contract_address,
            page=page,
            offset=100,
            sort="desc",
            start_block=last_block + 1 if last_block > 0 else 0,
        )
        if not transfers:
            break

        total_processed += len(transfers)

        for tx in transfers:
            try:
                value_raw = float(tx.get("value", 0))
                decimals = int(tx.get("tokenDecimal", 18))
                value = value_raw / (10 ** decimals)

                # 估算美元价值
                value_usd = round(value * price_usd, 2) if price_usd > 0 else None

                # 过滤：只保留大额转账
                if value_usd is not None and value_usd < LARGE_TRANSFER_THRESHOLD_USD:
                    continue

                from_addr = (tx.get("from", "") or "").lower()
                to_addr = (tx.get("to", "") or "").lower()

                from_exchange = exchange_map.get(from_addr)
                to_exchange = exchange_map.get(to_addr)

                from_label = "exchange" if from_exchange else "unknown"
                to_label = "exchange" if to_exchange else "unknown"

                is_to_exchange = to_exchange is not None

                # 告警模式：只保留转入交易所的
                if alarm_only and not is_to_exchange:
                    continue

                block_ts = datetime.fromtimestamp(
                    int(tx.get("timeStamp", 0)), tz=timezone.utc
                )

                all_transfers.append({
                    "asset_id": asset_id,
                    "chain": chain,
                    "contract_address": contract_address,
                    "tx_hash": tx.get("hash", ""),
                    "from_address": from_addr,
                    "to_address": to_addr,
                    "value": value,
                    "value_usd": value_usd,
                    "from_label": from_label,
                    "to_label": to_label,
                    "from_exchange": from_exchange,
                    "to_exchange": to_exchange,
                    "block_number": int(tx.get("blockNumber", 0)),
                    "block_timestamp": block_ts,
                    "is_to_exchange": is_to_exchange,
                })
            except (ValueError, TypeError):
                continue

        if len(transfers) < 100:
            break

    if dry_run:
        to_exchange_count = sum(1 for t in all_transfers if t["is_to_exchange"])
        label = "告警" if alarm_only else "大额"
        print(f"  [{symbol}] {chain}: {total_processed} 条转账, {to_exchange_count} 条{label} (dry-run)")
        return {"asset_id": asset_id, "symbol": symbol, "processed": total_processed, "written": 0}

    written = save_transfers(conn, all_transfers)
    to_exchange_count = sum(1 for t in all_transfers if t["is_to_exchange"])
    label = "告警" if alarm_only else "大额"
    print(f"  [{symbol}] {chain}: {total_processed} 条转账, {to_exchange_count} 条{label}, 写入 {written} 条")

    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "processed": total_processed,
        "written": written,
    }


def main():
    parser = argparse.ArgumentParser(description="链上大额转账监控")
    parser.add_argument("--asset-id", type=int, default=None, help="指定资产 ID")
    parser.add_argument("--chain", type=str, default=None, help="指定链（eth/bsc）")
    parser.add_argument("--limit", type=int, default=50, help="最大处理资产数")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写入")
    parser.add_argument("--alarm-only", action="store_true", help="告警模式：只存储转入交易所的大额转账")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    with psycopg.connect(settings.database_url) as conn:
        assets = get_asset_contracts(conn, args.asset_id)

        if args.chain:
            assets = [a for a in assets if a["chain"] == args.chain]
        if args.limit > 0:
            assets = assets[:args.limit]

        print(f"共 {len(assets)} 个资产待监控\n")

        chain_clients = {}
        chain_exchanges = {}

        total_processed = 0
        total_written = 0
        t0 = time.time()

        for i, asset in enumerate(assets, 1):
            chain = asset["chain"]

            if chain not in chain_clients:
                client = get_client(chain)
                if not client:
                    print(f"  [{i}/{len(assets)}] 跳过 {chain}: 无 API Key")
                    continue
                chain_clients[chain] = client
                chain_exchanges[chain] = get_exchange_map(conn, chain)

            client = chain_clients[chain]
            exchanges = chain_exchanges[chain]

            result = collect_transfers(
                conn, client, asset, exchanges,
                dry_run=args.dry_run,
                alarm_only=args.alarm_only,
            )
            total_processed += result.get("processed", 0)
            total_written += result.get("written", 0)

        elapsed = time.time() - t0
        label = "告警" if args.alarm_only else "大额"
        print(f"\n完成: 处理 {total_processed} 条转账, {label} {total_written} 条, 耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()