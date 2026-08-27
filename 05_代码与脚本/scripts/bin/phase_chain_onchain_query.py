"""
Phase 1: 链上数据按需查询。
投研时针对指定资产，拉取最新链上数据（持仓 + 大额转账 + 交易所钱包关联）。
先查缓存，缓存未命中再实时拉取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone

import psycopg
import psycopg.rows

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.etherscan_client import EtherscanClient, get_client


def get_asset_info(conn, asset_id: int) -> dict | None:
    """获取资产基本信息及合约地址。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
                   m.chain, m.contract_address
            FROM core.asset a
            INNER JOIN core.asset_contract_map m ON m.asset_id = a.asset_id
            WHERE a.asset_id = %s AND a.status = 'active'
        """, (asset_id,))
        rows = [dict(r) for r in cur.fetchall()]
    return rows if rows else None


def get_cached_snapshot(conn, asset_id: int) -> list[dict]:
    """获取本地缓存的持仓快照（今天的）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT * FROM biz.onchain_holder_snapshot
            WHERE asset_id = %s AND snapshot_date = CURRENT_DATE
            ORDER BY chain
        """, (asset_id,))
        return [dict(r) for r in cur.fetchall()]


def get_cached_transfers(conn, asset_id: int, limit: int = 20) -> list[dict]:
    """获取本地缓存的大额转账（最近 7 天）。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""
            SELECT * FROM biz.onchain_transfer_log
            WHERE asset_id = %s
              AND block_timestamp >= NOW() - INTERVAL '7 days'
            ORDER BY block_timestamp DESC
            LIMIT %s
        """, (asset_id, limit))
        return [dict(r) for r in cur.fetchall()]


def fetch_holder_snapshot(
    conn, client: EtherscanClient, asset: dict, chain: str,
) -> dict | None:
    """实时拉取持仓快照。"""
    contract_address = asset["contract_address"]
    if not contract_address:
        return None

    all_holders = []
    for page in range(1, 6):
        holders = client.get_token_holders(contract_address, page=page, offset=100)
        if not holders:
            break
        all_holders.extend(holders)
        if len(holders) < 100:
            break

    if not all_holders:
        return None

    total_supply = sum(float(h.get("balance", 0)) for h in all_holders)
    if total_supply == 0:
        return None

    top10_balance = sum(float(h.get("balance", 0)) for h in all_holders[:10])
    top50_balance = sum(float(h.get("balance", 0)) for h in all_holders[:50])
    top100_balance = sum(float(h.get("balance", 0)) for h in all_holders[:100])

    return {
        "chain": chain,
        "total_holders": len(all_holders),
        "top10_concentration": round(top10_balance / total_supply * 100, 2),
        "top50_concentration": round(top50_balance / total_supply * 100, 2),
        "top100_concentration": round(top100_balance / total_supply * 100, 2),
        "top_holders": [
            {
                "address": h.get("address", ""),
                "balance": float(h.get("balance", 0)),
                "share_pct": round(float(h.get("balance", 0)) / total_supply * 100, 2),
            }
            for h in all_holders[:10]
        ],
    }


def fetch_transfers(
    client: EtherscanClient, contract_address: str, limit: int = 20,
) -> list[dict]:
    """实时拉取最新大额转账。"""
    transfers = client.get_token_transfers(contract_address, page=1, offset=limit, sort="desc")
    result = []
    for tx in transfers:
        try:
            value_raw = float(tx.get("value", 0))
            decimals = int(tx.get("tokenDecimal", 18))
            value = value_raw / (10 ** decimals)
            result.append({
                "tx_hash": tx.get("hash", ""),
                "from_address": (tx.get("from", "") or "").lower(),
                "to_address": (tx.get("to", "") or "").lower(),
                "value": value,
                "block_number": int(tx.get("blockNumber", 0)),
                "block_timestamp": datetime.fromtimestamp(
                    int(tx.get("timeStamp", 0)), tz=timezone.utc
                ).isoformat(),
            })
        except (ValueError, TypeError):
            continue
    return result


def main():
    parser = argparse.ArgumentParser(description="链上数据按需查询")
    parser.add_argument("--asset-id", type=int, required=True, help="资产 ID")
    parser.add_argument("--force", action="store_true", help="强制刷新，忽略缓存")
    parser.add_argument("--output-json", action="store_true", default=True,
                        help="以 JSON 格式输出（默认）")
    args = parser.parse_args()

    try:
        _run(args)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)[:500]}))
        sys.exit(1)


def _run(args):

    settings = get_settings(require_database=True)
    t0 = time.time()

    with get_connection(settings.database_url) as conn:
        assets = get_asset_info(conn, args.asset_id)
        if not assets:
            print(json.dumps({"status": "error", "message": "资产不存在或无合约地址"}))
            sys.exit(1)

        result = {
            "status": "ok",
            "asset_id": args.asset_id,
            "symbol": assets[0]["canonical_symbol"],
            "name": assets[0]["canonical_name"],
            "from_cache": False,
            "chains": {},
            "transfers": [],
            "elapsed_ms": 0,
        }

        # 先查缓存
        if not args.force:
            cached = get_cached_snapshot(conn, args.asset_id)
            if cached:
                result["from_cache"] = True
                for c in cached:
                    chain = c["chain"]
                    result["chains"][chain] = {
                        "top10_concentration": float(c["top10_concentration"]) if c["top10_concentration"] else None,
                        "top50_concentration": float(c["top50_concentration"]) if c["top50_concentration"] else None,
                        "top100_concentration": float(c["top100_concentration"]) if c["top100_concentration"] else None,
                        "total_holders": c["total_holders"],
                        "exchange_wallet_pct": float(c["exchange_wallet_pct"]) if c["exchange_wallet_pct"] else None,
                        "holder_change_7d": c["holder_change_7d"],
                        "snapshot_date": str(c["snapshot_date"]),
                    }

                cached_tx = get_cached_transfers(conn, args.asset_id)
                result["transfers"] = [
                    {
                        "tx_hash": t["tx_hash"],
                        "from_address": t["from_address"],
                        "to_address": t["to_address"],
                        "value": float(t["value"]),
                        "value_usd": float(t["value_usd"]) if t["value_usd"] else None,
                        "is_to_exchange": t["is_to_exchange"],
                        "to_exchange": t["to_exchange"],
                        "block_timestamp": str(t["block_timestamp"]) if t["block_timestamp"] else None,
                    }
                    for t in cached_tx
                ]

                result["elapsed_ms"] = int((time.time() - t0) * 1000)
                print(json.dumps(result, ensure_ascii=False, default=str))
                return

        # 缓存未命中，实时拉取
        holder_fetched = False
        for asset in assets:
            chain = asset["chain"]
            client = get_client(chain)
            if not client:
                continue

            contract_address = asset["contract_address"]

            # 持仓快照（需要 Etherscan Pro 订阅）
            snapshot = fetch_holder_snapshot(conn, client, asset, chain)
            if snapshot:
                result["chains"][chain] = snapshot
                holder_fetched = True

            # 大额转账
            transfers = fetch_transfers(client, contract_address)
            result["transfers"].extend(transfers)

        if not holder_fetched and result["transfers"]:
            result["_note"] = "持仓数据需要 Etherscan Pro 订阅，仅返回了大额转账记录"

        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()