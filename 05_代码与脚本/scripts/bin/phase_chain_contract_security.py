#!/usr/bin/env python3
"""合约安全扫描：遍历 core.asset_contract 按 chain 分流扫描。

EVM → GoPlus（免费档），Solana → RugCheck + SolanaClient 兜底。
结果 UPSERT 到 biz.asset_contract_security（asset_id 唯一）。

用法：
    python phase_chain_contract_security.py --limit 50
    python phase_chain_contract_security.py --chain solana
    python phase_chain_contract_security.py --chain bsc --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.contract_security_client import ContractSecurityClient

UPSERT_SQL = """
INSERT INTO biz.asset_contract_security (
    asset_id, chain, contract_addr, source, source_status,
    is_honeypot, is_open_source, is_mintable, can_take_back_ownership,
    hidden_owner, is_blacklisted, freeze_authority, mint_authority,
    buy_tax, sell_tax, lp_locked_pct, top_holders_pct,
    holder_count, creator_percent, risk_score, raw_json, scanned_at
) VALUES (
    %(asset_id)s, %(chain)s, %(contract_addr)s, %(source)s, %(source_status)s,
    %(is_honeypot)s, %(is_open_source)s, %(is_mintable)s, %(can_take_back_ownership)s,
    %(hidden_owner)s, %(is_blacklisted)s, %(freeze_authority)s, %(mint_authority)s,
    %(buy_tax)s, %(sell_tax)s, %(lp_locked_pct)s, %(top_holders_pct)s,
    %(holder_count)s, %(creator_percent)s, %(risk_score)s, %(raw_json)s, NOW()
)
ON CONFLICT (asset_id) DO UPDATE SET
    source = EXCLUDED.source, source_status = EXCLUDED.source_status,
    is_honeypot = EXCLUDED.is_honeypot, is_open_source = EXCLUDED.is_open_source,
    is_mintable = EXCLUDED.is_mintable, can_take_back_ownership = EXCLUDED.can_take_back_ownership,
    hidden_owner = EXCLUDED.hidden_owner, is_blacklisted = EXCLUDED.is_blacklisted,
    freeze_authority = EXCLUDED.freeze_authority, mint_authority = EXCLUDED.mint_authority,
    buy_tax = EXCLUDED.buy_tax, sell_tax = EXCLUDED.sell_tax,
    lp_locked_pct = EXCLUDED.lp_locked_pct, top_holders_pct = EXCLUDED.top_holders_pct,
    holder_count = EXCLUDED.holder_count, creator_percent = EXCLUDED.creator_percent,
    risk_score = EXCLUDED.risk_score, raw_json = EXCLUDED.raw_json,
    scanned_at = NOW()
"""

# 简化的链名映射（core.asset_contract.chain 字段值 → scan 用的 chain）
CHAIN_MAP = {
    "ethereum": "ethereum", "eth": "ethereum",
    "bsc": "bsc", "binance": "bsc",
    "base": "base",
    "polygon": "polygon", "matic": "polygon",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "avalanche": "avalanche", "avax": "avalanche",
    "optimism": "optimism", "op": "optimism",
    "solana": "solana", "sol": "solana",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="合约安全扫描")
    parser.add_argument("--limit", type=int, default=50, help="扫描资产数量上限")
    parser.add_argument("--chain", type=str, default=None, help="限定链（ethereum/bsc/solana 等）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    client = ContractSecurityClient()

    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # 读取有合约地址的资产
            sql = """
                SELECT DISTINCT ON (ac.asset_id)
                    ac.asset_id, ac.chain, ac.contract_address
                FROM core.asset_contract ac
                WHERE ac.contract_address IS NOT NULL
                  AND LENGTH(ac.contract_address) > 10
            """
            params: list = []
            if args.chain:
                sql += " AND LOWER(ac.chain) = LOWER(%s)"
                params.append(args.chain)
            sql += " ORDER BY ac.asset_id, ac.contract_address LIMIT %s"
            params.append(args.limit)

            cur.execute(sql, params)
            assets = cur.fetchall()

        print(f"待扫描资产: {len(assets)}")
        if not assets:
            print("无资产可扫描")
            return 0

        success = 0
        hit = 0
        skip = 0
        fail = 0
        t0 = time.time()

        for i, asset in enumerate(assets, 1):
            aid = asset["asset_id"]
            chain = CHAIN_MAP.get((asset["chain"] or "").lower(), asset["chain"] or "")
            addr = asset["contract_address"]
            print(f"  [{i}/{len(assets)}] asset_id={aid} chain={chain} {addr[:12]}... ", end="", flush=True)

            result = client.scan(aid, chain, addr)
            # raw_json 序列化
            if result.get("raw_json"):
                result["raw_json"] = json.dumps(result["raw_json"], ensure_ascii=False, default=str)

            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, result)
            conn.commit()

            status = result.get("source_status", "?")
            if status == "hit":
                hit += 1
                print(f"✅ {result.get('source')}")
            elif status == "not_cached":
                skip += 1
                print(f"⚠️ {status}（免费层未缓存）")
            elif status == "na":
                skip += 1
                print(f"⬜ {status}（该链暂不支持）")
            else:
                fail += 1
                print(f"❌ {status}")

            if i < len(assets):
                time.sleep(0.3)  # 限流

            if i % 20 == 0:
                elapsed = time.time() - t0
                print(f"  -- 进度 {i}/{len(assets)}, hit={hit}, skip={skip}, fail={fail} --")

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f"扫描完成: hit={hit}, skip={skip}, fail={fail}, 耗时 {elapsed:.1f}s")
        print(f"{'=' * 60}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
