"""Phase A: Build core.asset_contract, dedup assets, create biz.coin_basic.

Steps:
  1. Create core.asset_contract table
  2. Populate from CMC platform data (7,165 assets with contract info)
  3. Populate from DL chain data
  4. Merge duplicate assets (same symbol + similar name, or exact cmc_id match)
  5. Create biz.coin_basic consumption view
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

# UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

import psycopg
from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# ---------- chain normalization ----------

CHAIN_NORMALIZE = {
    "ethereum": "ethereum",
    "bnb smart chain (bep20)": "bsc",
    "binance": "bsc",
    "solana": "solana",
    "base": "base",
    "polygon": "polygon",
    "arbitrum": "arbitrum",
    "ton": "ton",
    "avalanche c-chain": "avalanche",
    "avalanche": "avalanche",
    "robinhood chain": "robinhood",
    "sui network": "sui",
    "sui": "sui",
    "bittensor": "bittensor",
    "osmosis": "osmosis",
    "tron20": "tron",
    "multiversx": "multiversx",
    "cardano": "cardano",
    "cronos": "cronos",
    "icp": "icp",
    "kaia": "kaia",
    "hyperliquid": "hyperliquid",
    "hyperliquid l1": "hyperliquid",
    "xrp ledger": "xrpl",
    "aptos": "aptos",
    "optimism": "optimism",
    "near": "near",
    "anubis": "anubis",
    "hyperevm": "hyperevm",
    "fantom": "fantom",
    "blast": "blast",
    "sonic": "sonic",
    "zksync era": "zksync",
    "pulse": "pulsechain",
    "klaytn": "klaytn",
    "scroll": "scroll",
    "monad": "monad",
    "berachain": "berachain",
    "kava": "kava",
    "multi-chain": None,  # skip multi-chain
}


def normalize_chain(name: str | None) -> str | None:
    if not name:
        return None
    return CHAIN_NORMALIZE.get(name.strip().lower(), name.strip().lower())


# CoinGecko platform key → 归一化链名（与 core.asset_contract 现有命名一致）。
# 仅收录项目当前已识别/可爬取的链，未知 key 跳过，避免引入脏链名。
CG_CHAIN_MAP = {
    "ethereum": "ethereum",
    "binance-smart-chain": "bsc",
    "bnb-smart-chain": "bsc",
    "solana": "solana",
    "base": "base",
    "polygon-pos": "polygon",
    "polygon": "polygon",
    "arbitrum-one": "arbitrum",
    "arbitrum": "arbitrum",
    "optimistic-ethereum": "optimism",
    "optimism": "optimism",
    "avalanche": "avalanche",
    "avalanche-c-chain": "avalanche",
    "fantom": "fantom",
    "tron": "tron",
    "tron20": "tron",
    "the-open-network": "ton",
    "ton": "ton",
    "aptos": "aptos",
    "sui": "sui",
    "near-protocol": "near",
    "near": "near",
    "cronos": "cronos",
    "osmosis": "osmosis",
    "cardano": "cardano",
    "zksync-era": "zksync",
    "zksync": "zksync",
    "scroll": "scroll",
    "blast": "blast",
    "sonic": "sonic",
    "berachain": "berachain",
    "monad": "monad",
    "hyperliquid": "hyperliquid",
    "sei-network": "sei",
    "injective": "injective",
    "kaia": "kaia",
    "klay-token": "klaytn",
    "pulsechain": "pulsechain",
    "kava": "kava",
    "multiversx": "multiversx",
    "bittensor": "bittensor",
    "robinhood-chain": "robinhood",
    "anubis": "anubis",
    "hyperevm": "hyperevm",
}


# ===================== STEP 1: CREATE TABLE =====================

CREATE_ASSET_CONTRACT = """
DROP TABLE IF EXISTS core.asset_contract CASCADE;

CREATE TABLE core.asset_contract (
    contract_id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL REFERENCES core.asset(asset_id),
    chain TEXT NOT NULL,
    contract_address TEXT NOT NULL,
    decimals INTEGER,
    is_native BOOLEAN NOT NULL DEFAULT FALSE,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    source_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chain, contract_address)
);

CREATE INDEX IF NOT EXISTS idx_asset_contract_asset_id ON core.asset_contract(asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_contract_chain ON core.asset_contract(chain);
"""


def step1_create_table(settings) -> None:
    statements = [s.strip() for s in CREATE_ASSET_CONTRACT.split(";") if s.strip()]
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        conn.commit()
    print("[Step 1] core.asset_contract 表创建完成")


# ===================== STEP 2: POPULATE FROM CMC =====================

POPULATE_FROM_CMC = """
INSERT INTO core.asset_contract (asset_id, chain, contract_address, is_primary, source_code)
SELECT
    asm.asset_id,
    CASE
        WHEN LOWER(m.platform_name) IN ('ethereum', 'ethereum (erc20)') THEN 'ethereum'
        WHEN LOWER(m.platform_name) IN ('bnb smart chain (bep20)', 'binance smart chain') THEN 'bsc'
        WHEN LOWER(m.platform_name) IN ('solana', 'solana (spl)') THEN 'solana'
        WHEN LOWER(m.platform_name) = 'base' THEN 'base'
        WHEN LOWER(m.platform_name) IN ('polygon', 'polygon pos') THEN 'polygon'
        WHEN LOWER(m.platform_name) IN ('arbitrum', 'arbitrum one') THEN 'arbitrum'
        WHEN LOWER(m.platform_name) = 'ton' THEN 'ton'
        WHEN LOWER(m.platform_name) IN ('avalanche c-chain', 'avalanche') THEN 'avalanche'
        WHEN LOWER(m.platform_name) = 'robinhood chain' THEN 'robinhood'
        WHEN LOWER(m.platform_name) IN ('sui network', 'sui') THEN 'sui'
        WHEN LOWER(m.platform_name) = 'bittensor' THEN 'bittensor'
        WHEN LOWER(m.platform_name) = 'osmosis' THEN 'osmosis'
        WHEN LOWER(m.platform_name) IN ('tron20', 'tron') THEN 'tron'
        WHEN LOWER(m.platform_name) = 'multiversx' THEN 'multiversx'
        WHEN LOWER(m.platform_name) = 'cardano' THEN 'cardano'
        WHEN LOWER(m.platform_name) = 'cronos' THEN 'cronos'
        WHEN LOWER(m.platform_name) = 'icp' THEN 'icp'
        WHEN LOWER(m.platform_name) = 'kaia' THEN 'kaia'
        WHEN LOWER(m.platform_name) IN ('hyperliquid', 'hyperliquid l1') THEN 'hyperliquid'
        WHEN LOWER(m.platform_name) = 'xrp ledger' THEN 'xrpl'
        WHEN LOWER(m.platform_name) = 'aptos' THEN 'aptos'
        WHEN LOWER(m.platform_name) = 'optimism' THEN 'optimism'
        WHEN LOWER(m.platform_name) = 'near' THEN 'near'
        WHEN LOWER(m.platform_name) = 'fantom' THEN 'fantom'
        WHEN LOWER(m.platform_name) = 'blast' THEN 'blast'
        WHEN LOWER(m.platform_name) = 'sonic' THEN 'sonic'
        WHEN LOWER(m.platform_name) = 'zksync era' THEN 'zksync'
        WHEN LOWER(m.platform_name) = 'pulse' THEN 'pulsechain'
        WHEN LOWER(m.platform_name) = 'klaytn' THEN 'klaytn'
        WHEN LOWER(m.platform_name) = 'scroll' THEN 'scroll'
        WHEN LOWER(m.platform_name) = 'monad' THEN 'monad'
        WHEN LOWER(m.platform_name) = 'berachain' THEN 'berachain'
        WHEN LOWER(m.platform_name) = 'kava' THEN 'kava'
        WHEN LOWER(m.platform_name) = 'anubis' THEN 'anubis'
        WHEN LOWER(m.platform_name) = 'hyperevm' THEN 'hyperevm'
        ELSE LOWER(m.platform_name)
    END AS chain,
    LOWER(m.token_address) AS contract_address,
    TRUE AS is_primary,
    'cmc' AS source_code
FROM src_cmc.cmc_asset_map m
INNER JOIN core.asset_source_map asm
    ON asm.source_code = 'cmc'
    AND asm.source_asset_key = m.cmc_id::text
WHERE m.platform_name IS NOT NULL
  AND m.token_address IS NOT NULL
  AND m.token_address != ''
  AND LOWER(m.platform_name) != 'multi-chain'
ON CONFLICT (chain, contract_address) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    is_primary = TRUE,
    source_code = 'cmc',
    updated_at = NOW()
"""


def step2_populate_cmc(settings, limit: int = 0) -> None:
    """Populate from CMC: single batch insert with chain normalization."""
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(POPULATE_FROM_CMC)
            total = cur.rowcount
        conn.commit()
    print(f"[Step 2] CMC 合约填充完成: {total} 条")


# ===================== STEP 3: POPULATE FROM DL =====================

POPULATE_FROM_DL = """
INSERT INTO core.asset_contract (asset_id, chain, contract_address, is_primary, source_code)
WITH dl AS (
    SELECT
        asm.asset_id,
        p.chain AS raw_chain,
        p.address AS raw_address,
        -- DefiLlama 的 address 常为「链:地址」格式（如 bsc:0x...），
        -- 优先用前缀作为该合约所属链（比协议主链 raw_chain 更准确）。
        CASE
            WHEN position(':' in p.address) > 0
                 AND split_part(p.address, ':', 1) ~ '^[a-zA-Z][a-zA-Z0-9_-]*$'
            THEN split_part(p.address, ':', 1)
            ELSE NULL
        END AS addr_chain,
        CASE
            WHEN position(':' in p.address) > 0
                 AND split_part(p.address, ':', 1) ~ '^[a-zA-Z][a-zA-Z0-9_-]*$'
            THEN substring(p.address from position(':' in p.address) + 1)
            ELSE p.address
        END AS contract_address
    FROM src_dl.protocol_list p
    INNER JOIN core.asset_source_map asm
        ON asm.source_code = 'dl'
        AND asm.source_asset_key = p.protocol_id
    WHERE p.address IS NOT NULL
      AND p.address != ''
)
SELECT
    asset_id,
    CASE
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('ethereum', 'ethereum (erc20)', 'eth') THEN 'ethereum'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('binance', 'bsc', 'bnb smart chain', 'bnb') THEN 'bsc'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'solana' THEN 'solana'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'base' THEN 'base'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('polygon', 'polygon pos', 'matic') THEN 'polygon'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('arbitrum', 'arbitrum one', 'arb', 'arbirtum') THEN 'arbitrum'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'ton' THEN 'ton'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('avalanche', 'avalanche c-chain', 'avax') THEN 'avalanche'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'sui' THEN 'sui'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('fantom', 'ftm') THEN 'fantom'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'cronos' THEN 'cronos'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'aptos' THEN 'aptos'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('optimism', 'op') THEN 'optimism'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'near' THEN 'near'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'sonic' THEN 'sonic'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('zksync era', 'zksync', 'era') THEN 'zksync'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('pulse', 'pulsechain') THEN 'pulsechain'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'klaytn' THEN 'klaytn'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'scroll' THEN 'scroll'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'monad' THEN 'monad'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'berachain' THEN 'berachain'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'kava' THEN 'kava'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'blast' THEN 'blast'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('hyperliquid', 'hyperliquid l1') THEN 'hyperliquid'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) = 'cardano' THEN 'cardano'
        WHEN LOWER(COALESCE(addr_chain, raw_chain)) IN ('robinhood chain', 'robinhood') THEN 'robinhood'
        ELSE LOWER(COALESCE(addr_chain, raw_chain))
    END AS chain,
    LOWER(contract_address) AS contract_address,
    FALSE AS is_primary,
    'dl' AS source_code
FROM dl
WHERE LOWER(contract_address) <> '0x0000000000000000000000000000000000000000'
  AND (addr_chain IS NOT NULL OR LOWER(raw_chain) != 'multi-chain')
ON CONFLICT (chain, contract_address) DO NOTHING
"""


def step3_populate_dl(settings) -> None:
    """Populate from DL protocols that have contract addresses."""
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(POPULATE_FROM_DL)
            total = cur.rowcount
        conn.commit()
    print(f"[Step 3] DL 合约填充完成: {total} 条")


# ===================== STEP 3b: POPULATE FROM CG PLATFORMS =====================


def step3b_populate_cg(settings) -> None:
    """从 CoinGecko platforms 补齐 CMC/DL 未覆盖的多链合约地址。

    CMC 只收录部分链、DL 只收录有 TVL 的协议，导致像 ROBO 这类多链代币
    的 Base/BSC 合约缺失。CG 的 platforms 字段是 {chain: contract} 字典，
    用它补充 core.asset_contract，同时用 ON CONFLICT DO NOTHING 避免覆盖
    已存在的 CMC 主合约（is_primary）。
    """
    with get_connection(settings.database_url) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT asm.asset_id, ci.platforms
                FROM core.asset_source_map asm
                INNER JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
                WHERE asm.source_code = 'cg'
                """
            )
            rows = cur.fetchall()

        seen: set[tuple[str, str]] = set()
        insert_rows: list[tuple[int, str, str]] = []
        for row in rows:
            platforms = row["platforms"] or {}
            if not isinstance(platforms, dict):
                continue
            for platform_key, addr in platforms.items():
                chain = CG_CHAIN_MAP.get((platform_key or "").strip().lower())
                if not chain:
                    continue
                addr = (addr or "").strip().lower()
                if not addr:
                    continue
                key = (chain, addr)
                if key in seen:
                    continue
                seen.add(key)
                insert_rows.append((row["asset_id"], chain, addr))

        if insert_rows:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO core.asset_contract
                        (asset_id, chain, contract_address, is_primary, source_code)
                    VALUES (%s, %s, %s, FALSE, 'cg')
                    ON CONFLICT (chain, contract_address) DO NOTHING
                    """,
                    insert_rows,
                )
            conn.commit()
        print(f"[Step 3b] CG 合约填充完成: {len(insert_rows)} 条（唯一）")


# ===================== STEP 4: DEDUP ASSETS =====================


def step4_dedup_assets(settings, dry_run: bool = False) -> None:
    """Merge duplicate assets where:
    - Same symbol, same name (case-insensitive) → merge into oldest asset_id
    - This handles the CG artifacts where bootstrap created dupes
    """
    with get_connection(settings.database_url) as conn:
        # Find exact (symbol, name) duplicates
        with conn.cursor() as cur:
            cur.execute("""
                SELECT UPPER(canonical_symbol) AS sym,
                       UPPER(canonical_name) AS nam,
                       COUNT(*) AS cnt,
                       MIN(asset_id) AS keep_id,
                       array_agg(asset_id ORDER BY asset_id) AS all_ids
                FROM core.asset
                GROUP BY UPPER(canonical_symbol), UPPER(canonical_name)
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC
            """)
            dupes = [
                {
                    "sym": row[0],
                    "nam": row[1],
                    "cnt": row[2],
                    "keep_id": row[3],
                    "all_ids": row[4],
                }
                for row in cur.fetchall()
            ]

        if not dupes:
            print("[Step 4] 无重复资产需要合并")
            return

        total_merged = 0
        for d in dupes:
            keep_id = d["keep_id"]
            # IDs to merge (exclude keep_id)
            merge_ids = [aid for aid in d["all_ids"] if aid != keep_id]

            if dry_run:
                print(
                    f"  DRY-RUN: {d['sym']} / {d['nam']} → keep {keep_id}, merge {merge_ids}"
                )
                continue

            for old_id in merge_ids:
                with conn.cursor() as cur:
                    # Delete source_maps that would conflict at keep_id
                    cur.execute(
                        """
                        DELETE FROM core.asset_source_map
                        WHERE asset_id = %s
                          AND (source_code, source_asset_key) IN (
                              SELECT source_code, source_asset_key
                              FROM core.asset_source_map
                              WHERE asset_id = %s
                          )
                    """,
                        (old_id, keep_id),
                    )

                    # Move remaining source_maps
                    cur.execute(
                        """
                        UPDATE core.asset_source_map
                        SET asset_id = %s, updated_at = NOW()
                        WHERE asset_id = %s
                    """,
                        (keep_id, old_id),
                    )

                    # Delete doc_source_entries that would conflict at keep_id
                    cur.execute(
                        """
                        DELETE FROM biz.doc_source_entry
                        WHERE asset_id = %s
                          AND (entity_type, entry_url) IN (
                              SELECT entity_type, entry_url
                              FROM biz.doc_source_entry
                              WHERE asset_id = %s
                          )
                    """,
                        (old_id, keep_id),
                    )

                    # Move remaining doc_source_entries
                    cur.execute(
                        """
                        UPDATE biz.doc_source_entry
                        SET asset_id = %s, updated_at = NOW()
                        WHERE asset_id = %s
                    """,
                        (keep_id, old_id),
                    )

                    # Delete conflicting contracts
                    cur.execute(
                        """
                        DELETE FROM core.asset_contract
                        WHERE asset_id = %s
                          AND (chain, contract_address) IN (
                              SELECT chain, contract_address
                              FROM core.asset_contract
                              WHERE asset_id = %s
                          )
                    """,
                        (old_id, keep_id),
                    )

                    # Move remaining contracts
                    cur.execute(
                        """
                        UPDATE core.asset_contract
                        SET asset_id = %s, updated_at = NOW()
                        WHERE asset_id = %s
                    """,
                        (keep_id, old_id),
                    )

                    # Now safe to delete old asset
                    cur.execute("DELETE FROM core.asset WHERE asset_id = %s", (old_id,))

                conn.commit()
                total_merged += 1

        print(f"[Step 4] 资产去重: {len(dupes)} 组, 合并 {total_merged} 条")


# ===================== STEP 5: BUILD COIN_BASIC =====================

CREATE_COIN_BASIC = """
CREATE TABLE IF NOT EXISTS biz.coin_basic (
    asset_id BIGINT PRIMARY KEY REFERENCES core.asset(asset_id),
    cmc_id BIGINT,
    defillama_slug TEXT,
    coin_symbol TEXT NOT NULL,
    coin_name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    main_chain TEXT,
    primary_contract_address TEXT,
    official_website TEXT,
    description_short TEXT,
    logo_url TEXT,
    mapping_status TEXT NOT NULL DEFAULT 'active',
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_coin_basic_symbol ON biz.coin_basic(coin_symbol);
CREATE INDEX IF NOT EXISTS idx_coin_basic_cmc_id ON biz.coin_basic(cmc_id);
CREATE INDEX IF NOT EXISTS idx_coin_basic_type ON biz.coin_basic(asset_type);
"""

REFRESH_COIN_BASIC = """
INSERT INTO biz.coin_basic (
    asset_id, cmc_id, defillama_slug,
    coin_symbol, coin_name, asset_type,
    main_chain, primary_contract_address,
    official_website, description_short, logo_url,
    mapping_status, last_refreshed_at
)
SELECT
    a.asset_id,
    -- CMC id
    (SELECT cmc.source_asset_key::bigint
     FROM core.asset_source_map cmc
     WHERE cmc.asset_id = a.asset_id AND cmc.source_code = 'cmc'
     LIMIT 1) AS cmc_id,
    -- DL slug
    (SELECT dl.source_asset_key
     FROM core.asset_source_map dl
     WHERE dl.asset_id = a.asset_id AND dl.source_code = 'dl'
     LIMIT 1) AS defillama_slug,
    a.canonical_symbol AS coin_symbol,
    a.canonical_name AS coin_name,
    a.asset_type,
    -- Main chain (from contracts, prefer primary)
    (SELECT ac.chain
     FROM core.asset_contract ac
     WHERE ac.asset_id = a.asset_id
     ORDER BY ac.is_primary DESC, ac.contract_id
     LIMIT 1) AS main_chain,
    -- Primary contract address
    (SELECT ac.contract_address
     FROM core.asset_contract ac
     WHERE ac.asset_id = a.asset_id
     ORDER BY ac.is_primary DESC, ac.contract_id
     LIMIT 1) AS primary_contract_address,
    -- Official website (from doc_source_entry)
    (SELECT dse.entry_url
     FROM biz.doc_source_entry dse
     WHERE dse.asset_id = a.asset_id
       AND dse.entry_type = 'official_website'
       AND dse.is_primary = TRUE
     LIMIT 1) AS official_website,
    a.description_short,
    -- Logo (from cmc_asset_info)
    (SELECT info.logo
     FROM src_cmc.cmc_asset_info info
     WHERE info.cmc_id = (
         SELECT cmc.source_asset_key::bigint
         FROM core.asset_source_map cmc
         WHERE cmc.asset_id = a.asset_id AND cmc.source_code = 'cmc'
         LIMIT 1
     )) AS logo_url,
    'active' AS mapping_status,
    NOW() AS last_refreshed_at
FROM core.asset a
WHERE a.status = 'active'
ON CONFLICT (asset_id) DO UPDATE SET
    cmc_id = EXCLUDED.cmc_id,
    defillama_slug = EXCLUDED.defillama_slug,
    coin_symbol = EXCLUDED.coin_symbol,
    coin_name = EXCLUDED.coin_name,
    asset_type = EXCLUDED.asset_type,
    main_chain = EXCLUDED.main_chain,
    primary_contract_address = EXCLUDED.primary_contract_address,
    official_website = EXCLUDED.official_website,
    description_short = EXCLUDED.description_short,
    logo_url = EXCLUDED.logo_url,
    last_refreshed_at = NOW()
"""


def step5_build_coin_basic(settings) -> None:
    with get_connection(settings.database_url) as conn:
        statements = [s.strip() for s in CREATE_COIN_BASIC.split(";") if s.strip()]
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(REFRESH_COIN_BASIC)
            rowcount = cur.rowcount
        conn.commit()

    print(f"[Step 5] biz.coin_basic 构建完成: {rowcount} 条")


# ===================== MAIN =====================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase A: consolidate core data quality.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--step",
        type=str,
        default="all",
        help="Which step: create_table, populate_cmc, populate_dl, populate_cg, dedup, coin_basic, all",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings(require_database=True)
    steps = args.step.split(",")

    if "all" in steps or "create_table" in steps:
        step1_create_table(settings)

    if "all" in steps or "populate_cmc" in steps:
        step2_populate_cmc(settings)

    if "all" in steps or "populate_dl" in steps:
        step3_populate_dl(settings)

    if "all" in steps or "populate_cg" in steps:
        step3b_populate_cg(settings)

    if "all" in steps or "dedup" in steps:
        step4_dedup_assets(settings, dry_run=args.dry_run)

    if "all" in steps or "coin_basic" in steps:
        step5_build_coin_basic(settings)

    # Final stats
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM core.asset_contract")
            contracts = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM biz.coin_basic")
            cb = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM core.asset")
            assets = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(DISTINCT canonical_symbol) FROM core.asset
            """)
            uniq_sym = cur.fetchone()[0]

    print(f"\n{'=' * 50}")
    print(f"  阶段 A 完成统计")
    print(f"{'=' * 50}")
    print(f"  core.asset            : {assets} (唯一 symbol: {uniq_sym})")
    print(f"  core.asset_contract   : {contracts}")
    print(f"  biz.coin_basic        : {cb}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
