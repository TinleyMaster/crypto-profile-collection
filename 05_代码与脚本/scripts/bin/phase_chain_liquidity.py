#!/usr/bin/env python3
"""DEX 流动性扫描：双源（DexScreener + GeckoTerminal）聚合资产流动性。

用法：
    python phase_chain_liquidity.py [--limit 100] [--chain ethereum]
    python phase_chain_liquidity.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# prod: /app/scripts/bin/ → /app/; local: scripts/bin/ → scripts/src/
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import dexscreener_liquidity_client as dex_client
import geckoterminal_liquidity_client as geo_client

GECKO_SOLANA_LIKE = {"solana"}

UPSERT_SQL = """
INSERT INTO biz.asset_liquidity (
    asset_id, chain, pool_count, total_liquidity_usd, top_pool_share_pct,
    cex_listed, cex_exchanges, source, source_status, raw_json, scanned_at
) VALUES (
    %(asset_id)s, %(chain)s, %(pool_count)s, %(total_liquidity_usd)s, %(top_pool_share_pct)s,
    %(cex_listed)s, %(cex_exchanges)s, %(source)s, %(source_status)s,
    %(raw_json)s::jsonb, NOW()
)
ON CONFLICT (asset_id, chain) DO UPDATE SET
    chain = EXCLUDED.chain,
    pool_count = EXCLUDED.pool_count,
    total_liquidity_usd = EXCLUDED.total_liquidity_usd,
    top_pool_share_pct = EXCLUDED.top_pool_share_pct,
    cex_listed = EXCLUDED.cex_listed,
    cex_exchanges = EXCLUDED.cex_exchanges,
    source = EXCLUDED.source,
    source_status = EXCLUDED.source_status,
    raw_json = EXCLUDED.raw_json,
    scanned_at = NOW()
"""


@contextmanager
def _get_db():
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        yield conn


def query_asset_contracts(limit: int, chain: str | None = None) -> list[tuple[int, str, str]]:
    """返回 [(asset_id, chain, contract_address), ...]。"""
    with _get_db() as conn:
        with conn.cursor() as cur:
            if chain:
                cur.execute(
                    "SELECT asset_id, chain, contract_address FROM core.asset_contract "
                    "WHERE chain = %s AND contract_address IS NOT NULL "
                    "ORDER BY asset_id LIMIT %s",
                    (chain, limit),
                )
            else:
                cur.execute(
                    "SELECT asset_id, chain, contract_address FROM core.asset_contract "
                    "WHERE contract_address IS NOT NULL "
                    "ORDER BY asset_id LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def get_asset_derivatives(asset_id: int) -> dict | None:
    with _get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT available_exchanges FROM biz.asset_derivatives WHERE asset_id = %s",
                (asset_id,),
            )
            row = cur.fetchone()
    if not row or not row[0]:
        return None
    return {"available_exchanges": row[0]}


def build_result(aid: int, chain: str, addr: str, liq: dict) -> dict:
    """组装 UPSERT 参数，铁律：所有占位符 ⊆ result.keys()。"""
    deriv = get_asset_derivatives(aid)
    ex = (deriv or {}).get("available_exchanges") or []
    result = {
        "asset_id": aid,
        "chain": chain,
        "pool_count": liq.get("pool_count"),
        "total_liquidity_usd": liq.get("total_liquidity_usd"),
        "top_pool_share_pct": liq.get("top_pool_share_pct"),
        "cex_listed": bool(ex),
        "cex_exchanges": ex if ex else None,
        "source": liq.get("source"),
        "source_status": liq.get("source_status"),
        "raw_json": liq.get("raw_json"),
    }
    # 铁律守卫：占位符集 ⊆ result.keys()
    for _k in ("pool_count", "total_liquidity_usd", "top_pool_share_pct",
               "cex_listed", "cex_exchanges", "source", "source_status", "raw_json"):
        result.setdefault(_k, None)
    if result.get("raw_json") and not isinstance(result["raw_json"], str):
        result["raw_json"] = json.dumps(result["raw_json"], ensure_ascii=False, default=str)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="DEX 流动性扫描")
    parser.add_argument("--limit", type=int, default=100, help="扫描资产数量（默认100）")
    parser.add_argument("--chain", type=str, default=None, help="限定链（如 ethereum）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    assets = query_asset_contracts(limit=args.limit, chain=args.chain)
    print(f"待扫描资产: {len(assets)}")

    processed = 0
    failed = 0

    if args.dry_run:
        for aid, chain, addr in assets:
            if (chain or "").lower() in GECKO_SOLANA_LIKE:
                liq = geo_client.get_liquidity(chain, addr)
                if liq.get("source_status") in ("na", "error", "not_cached"):
                    liq = dex_client.get_liquidity(addr)
            else:
                liq = dex_client.get_liquidity(addr)
                if liq.get("source_status") == "error":
                    liq = geo_client.get_liquidity(chain, addr)
            result = build_result(aid, chain, addr, liq)
            print(json.dumps(result, ensure_ascii=False, default=str))
            processed += 1
    else:
        with _get_db() as conn:
            for aid, chain, addr in assets:
                if (chain or "").lower() in GECKO_SOLANA_LIKE:
                    liq = geo_client.get_liquidity(chain, addr)
                    if liq.get("source_status") in ("na", "error", "not_cached"):
                        liq = dex_client.get_liquidity(addr)
                else:
                    liq = dex_client.get_liquidity(addr)
                    if liq.get("source_status") == "error":
                        liq = geo_client.get_liquidity(chain, addr)
                result = build_result(aid, chain, addr, liq)
                try:
                    with conn.cursor() as cur:
                        cur.execute(UPSERT_SQL, result)
                    processed += 1
                except Exception as e:
                    print(f"  asset_id={aid} FAILED: {e}", file=sys.stderr)
                    failed += 1

    print(json.dumps({
        "status": "success" if failed == 0 else "partial",
        "processed": processed,
        "failed": failed,
        "total": len(assets),
    }, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
