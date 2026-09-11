"""Ingest CMC DEX token data into src_cmc DEX tables.

Populates:
  - src_cmc.cmc_dex_token                 (from /v1/dex/token)
  - src_cmc.cmc_dex_token_price_snapshot  (from /v1/dex/token/price)
  - src_cmc.cmc_dex_pool_snapshot         (from /v1/dex/token/pools)
  - src_cmc.cmc_dex_security_snapshot     (from /v1/dex/security/detail)

数据源：core.asset_contract 中带链 + 合约地址的资产（限定 CMC DEX 支持的链）。

Usage:
    python ingest_cmc_dex.py --limit 200
    python ingest_cmc_dex.py --dry-run
    python ingest_cmc_dex.py --chain ethereum --limit 50
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# CMC DEX 平台 id 与链名（platform-id 参数）：常用链映射
PLATFORM_IDS = {
    "ethereum": "ethereum",
    "bsc": "bsc",
    "solana": "solana",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "polygon": "polygon",
    "base": "base",
    "avalanche": "avalanche-c-chain",
    "tron": "tron",
    "ton": "ton",
    "sui": "sui",
    "aptos": "aptos",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest CMC DEX token data into src_cmc DEX tables."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max assets to process. Default: 200.",
    )
    parser.add_argument(
        "--chain",
        type=str,
        default=None,
        help="Restrict to a single chain (e.g. ethereum, bsc, solana).",
    )
    parser.add_argument(
        "--sections",
        default="token,price,pools,security",
        help="Comma-separated sections to run: token,price,pools,security.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse only, do not write database.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sections = {s.strip() for s in args.sections.split(",") if s.strip()}

    from crypto_research.clients.cmc_client import CMCClient
    from crypto_research.config import get_settings
    from crypto_research.db.upsert import load_sql
    from crypto_research.parsers.cmc_analysis import (
        parse_dex_pools_payload,
        parse_dex_security_payload,
        parse_dex_token_payload,
        parse_dex_token_price_payload,
    )

    settings = get_settings(require_database=not args.dry_run)
    client = CMCClient(settings)

    # 获取待处理资产（core.asset_contract 中带链 + 合约地址）
    targets: list[tuple[str, str, str, str]] = []  # (chain, contract, symbol, name)
    if not args.dry_run:
        from crypto_research.db.conn import get_connection

        chain_filter = ""
        params: list = []
        if args.chain:
            chain_filter = "AND LOWER(c.chain_name) = LOWER(%s)"
            params.append(args.chain)
        with get_connection(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT LOWER(c.chain_name) AS chain, ac.contract_address AS addr,
                           a.canonical_symbol, a.canonical_name
                    FROM core.asset_contract ac
                    JOIN core.chain c ON c.chain_id = ac.chain_id
                    JOIN core.asset a ON a.asset_id = ac.asset_id
                    WHERE ac.contract_address IS NOT NULL
                      AND LOWER(c.chain_name) = ANY(%s)
                      {chain_filter}
                    LIMIT %s
                    """,
                    [list(PLATFORM_IDS.keys())] + params + [args.limit],
                )
                targets = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    else:
        targets = [
            ("ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT", "Tether"),
        ]

    if not targets:
        print(json.dumps({"status": "noop", "reason": "no contract targets"}, ensure_ascii=False))
        return 0

    # ═══ 逐个拉取 ═══
    token_rows: list[tuple] = []
    price_rows: list[tuple] = []
    pool_rows: list[tuple] = []
    security_rows: list[tuple] = []
    inter_sleep = 1.0

    for chain, addr, symbol, name in targets:
        platform_id = PLATFORM_IDS.get(chain)
        if not platform_id:
            continue

        try:
            if "token" in sections:
                payload = client.get_dex_token(platform_id=platform_id, token_address=addr)
                parsed = parse_dex_token_payload(payload, platform_id, addr)
                if parsed:
                    token_rows.append(
                        (
                            parsed["platform_id"],
                            parsed["chain_name"] or chain,
                            parsed["token_address"],
                            parsed["symbol"] or symbol,
                            parsed["name"] or name,
                            parsed["decimals"],
                            parsed["project_url"],
                            parsed["logo"],
                            None,
                        )
                    )
                    time.sleep(inter_sleep)

            if "price" in sections:
                payload = client.get_dex_token_price(platform_id=platform_id, token_address=addr)
                parsed = parse_dex_token_price_payload(payload, platform_id, addr)
                if parsed:
                    price_rows.append(
                        (
                            parsed["platform_id"],
                            parsed["token_address"],
                            parsed["snapshot_time"],
                            parsed["chain_name"] or chain,
                            parsed["price_usd"],
                            parsed["market_cap"],
                            parsed["liquidity_usd"],
                            parsed["volume_24h"],
                            parsed["price_change_24h"],
                            None,
                        )
                    )
                    time.sleep(inter_sleep)

            if "pools" in sections:
                payload = client.get_dex_token_pools(platform_id=platform_id, token_address=addr)
                for parsed in parse_dex_pools_payload(payload, platform_id, addr):
                    pool_rows.append(
                        (
                            parsed["platform_id"],
                            parsed["token_address"],
                            parsed["snapshot_time"],
                            parsed["pool_address"],
                            parsed["dex_name"],
                            parsed["pair_name"],
                            parsed["liquidity_usd"],
                            parsed["volume_24h"],
                            parsed["fee_rate"],
                            parsed["chain_name"] or chain,
                            None,
                        )
                    )
                    time.sleep(inter_sleep)

            if "security" in sections:
                payload = client.get_dex_security(platform_id=platform_id, token_address=addr)
                parsed = parse_dex_security_payload(payload, platform_id, addr)
                if parsed:
                    security_rows.append(
                        (
                            parsed["platform_id"],
                            parsed["token_address"],
                            parsed["snapshot_time"],
                            parsed["chain_name"] or chain,
                            parsed["is_honeypot"],
                            parsed["buy_tax"],
                            parsed["sell_tax"],
                            parsed["can_take_back_ownership"],
                            parsed["owner_address"],
                            parsed["risk_level"],
                            json.dumps(parsed["security_flags"], ensure_ascii=False),
                            None,
                        )
                    )
                    time.sleep(inter_sleep)

        except Exception as e:
            print(f"[dex] {chain}/{addr[:12]}... 失败: {e}", file=sys.stderr)
            continue

        print(f"[dex] {chain}/{symbol} 完成", file=sys.stderr)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "targets": len(targets),
                    "token_rows": len(token_rows),
                    "price_rows": len(price_rows),
                    "pool_rows": len(pool_rows),
                    "security_rows": len(security_rows),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    insert_ingest_run_sql = load_sql("sys/insert_ingest_run.sql")
    finish_ingest_run_sql = load_sql("sys/finish_ingest_run.sql")
    insert_raw_sql = load_sql("raw/insert_api_response.sql")
    upsert_token_sql = load_sql("src_cmc/upsert_cmc_dex_token.sql")
    upsert_price_sql = load_sql("src_cmc/upsert_cmc_dex_token_price_snapshot.sql")
    upsert_pool_sql = load_sql("src_cmc/upsert_cmc_dex_pool_snapshot.sql")
    upsert_security_sql = load_sql("src_cmc/upsert_cmc_dex_security_snapshot.sql")

    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used")

    from crypto_research.db.conn import get_connection
    from crypto_research.db.upsert import execute_many, fetch_one
    from crypto_research.utils.hash_utils import md5_text

    with get_connection(settings.database_url) as conn:
        run_row = fetch_one(
            conn,
            insert_ingest_run_sql,
            (
                "cmc",
                "cmc_dex_snapshot",
                "WF_CMC_DEX",
                json.dumps(
                    {"limit": args.limit, "chain": args.chain, "sections": sorted(sections)},
                    ensure_ascii=False,
                ),
                f"{settings.cmc_base_url}/v1/dex/token",
            ),
        )
        run_id = run_row["run_id"]

        try:
            for chain, addr, _, _ in targets:
                request_key = f"{chain}|{addr}"
                fetch_one(
                    conn,
                    insert_raw_sql,
                    (
                        run_id,
                        "cmc",
                        "cmc_dex_snapshot",
                        request_key,
                        None,
                        "page:all",
                        None,
                        md5_text(request_key),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            if token_rows:
                execute_many(conn, upsert_token_sql, token_rows)
            if price_rows:
                execute_many(conn, upsert_price_sql, price_rows)
            if pool_rows:
                execute_many(conn, upsert_pool_sql, pool_rows)
            if security_rows:
                execute_many(conn, upsert_security_sql, security_rows)

            total = len(token_rows) + len(price_rows) + len(pool_rows) + len(security_rows)
            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "success",
                    200,
                    total,
                    total,
                    0,
                    None,
                    run_id,
                ),
            )

            print(
                json.dumps(
                    {
                        "status": "success",
                        "run_id": run_id,
                        "token_rows": len(token_rows),
                        "price_rows": len(price_rows),
                        "pool_rows": len(pool_rows),
                        "security_rows": len(security_rows),
                        "total": total,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except Exception as exc:
            conn.rollback()
            fetch_one(
                conn,
                finish_ingest_run_sql,
                (
                    "failed",
                    None,
                    None,
                    None,
                    None,
                    str(exc),
                    run_id,
                ),
            )
            raise


if __name__ == "__main__":
    raise SystemExit(main())