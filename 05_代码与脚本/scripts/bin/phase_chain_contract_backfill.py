"""链上合约地址回填。

从 CoinMarketCap /v2/cryptocurrency/info 拉取合约地址，
补全 core.asset_contract（即 core.asset_contract_map 底层表）中缺失的映射。

主要解决 FINDING-C：meme 币因缺少合约映射而无法进入链上转账/持仓/资金流采集。

用法：
    python phase_chain_contract_backfill.py                  # 默认补全全部缺失映射的 meme
    python phase_chain_contract_backfill.py --asset-type meme # 同上（显式）
    python phase_chain_contract_backfill.py --asset-id 11112  # 单个资产回填
    python phase_chain_contract_backfill.py --limit 100       # 只处理前 100 个
    python phase_chain_contract_backfill.py --dry-run         # 预览，不写入
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg
import psycopg.rows

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.clients.cmc_client import CMCClient  # noqa: E402


# CMC platform name/slug -> core.asset_contract.chain
# 仅包含 phase_chain_transfer_monitor 已支持的链
CHAIN_NAME_MAP = {
    "ethereum": "ethereum",
    "eth": "ethereum",
    "binance smart chain": "bsc",
    "bsc": "bsc",
    "binance-smart-chain": "bsc",
    "solana": "solana",
    "sol": "solana",
    "polygon": "polygon",
    "matic": "polygon",
    "matic-network": "polygon",
    "arbitrum": "arbitrum",
    "arbitrum one": "arbitrum",
    "arbitrum-one": "arbitrum",
    "base": "base",
    "optimism": "optimism",
    "op": "optimism",
    "avalanche": "avalanche",
    "avalanche c-chain": "avalanche",
    "avax": "avalanche",
    "avalanche-c-chain": "avalanche",
    "tron": "tron",
    "trx": "tron",
    "ton": "ton",
    "the open network": "ton",
    "the-open-network": "ton",
    "sui": "sui",
    "aptos": "aptos",
    "apt": "aptos",
}

SUPPORTED_CHAINS = frozenset({
    "ethereum", "bsc", "solana", "polygon", "arbitrum", "base",
    "optimism", "avalanche", "tron", "ton", "sui", "aptos",
})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill chain contract addresses from CMC into core.asset_contract."
    )
    parser.add_argument(
        "--asset-type",
        type=str,
        default="meme",
        help="只处理指定 asset_type 的资产。默认: meme。传空字符串''处理全部类型。",
    )
    parser.add_argument(
        "--asset-id",
        type=int,
        default=None,
        help="只回填单个 asset_id（覆盖 --asset-type/--limit）。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多处理多少个资产。默认不限。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="每批调用 CMC info 的 ID 数（CMC 上限约 100）。默认: 50。",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="批次间休眠秒数，控制 CMC 速率。默认: 2.0。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印，不写入数据库。",
    )
    return parser


def normalize_chain(platform: dict) -> str | None:
    """从 CMC platform 对象归一化到 core.asset_contract.chain。"""
    name = (platform.get("name") or platform.get("slug") or "").strip().lower()
    if not name:
        return None
    chain = CHAIN_NAME_MAP.get(name)
    if chain:
        return chain
    # 尝试把空格换成中划线再匹配
    chain = CHAIN_NAME_MAP.get(name.replace(" ", "-"))
    return chain


def fetch_missing_assets(
    conn: psycopg.Connection,
    asset_type: str | None,
    asset_id: int | None,
    limit: int | None,
) -> list[dict]:
    """查询缺少合约映射且已关联 cmc_id 的资产。

    返回 [{asset_id, cmc_id, canonical_symbol}, ...]
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if asset_id is not None:
            cur.execute("""
                SELECT a.asset_id, cb.cmc_id, a.canonical_symbol
                FROM core.asset a
                LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
                WHERE a.asset_id = %s AND a.status = 'active'
                LIMIT 1
            """, (asset_id,))
        else:
            params: list = []
            type_filter = ""
            if asset_type:
                type_filter = "AND a.asset_type = %s"
                params.append(asset_type)

            limit_clause = ""
            if limit:
                limit_clause = "LIMIT %s"
                params.append(limit)

            cur.execute(f"""
                SELECT a.asset_id, cb.cmc_id, a.canonical_symbol
                FROM core.asset a
                LEFT JOIN biz.coin_basic cb ON cb.asset_id = a.asset_id
                WHERE a.status = 'active'
                  AND a.asset_id NOT IN (
                      SELECT DISTINCT ac.asset_id FROM core.asset_contract ac
                  )
                  AND cb.cmc_id IS NOT NULL
                  {type_filter}
                ORDER BY COALESCE(a.market_cap, 0) DESC, a.asset_id
                {limit_clause}
            """, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def parse_contracts(
    payload: dict,
    asset_id_map: dict[int, int],
) -> list[dict]:
    """解析 CMC /v2/cryptocurrency/info 响应。

    返回可写入 core.asset_contract 的列表：
        [{asset_id, chain, contract_address}, ...]
    """
    rows: list[dict] = []
    data = payload.get("data") or {}
    for cmc_id_str, info in data.items():
        cmc_id = int(cmc_id_str)
        asset_id = asset_id_map.get(cmc_id)
        if not asset_id:
            continue

        for ca in info.get("contract_address") or []:
            addr = (ca.get("contract_address") or "").strip()
            if not addr:
                continue
            chain = normalize_chain(ca.get("platform") or {})
            if not chain or chain not in SUPPORTED_CHAINS:
                continue
            rows.append({
                "asset_id": asset_id,
                "chain": chain,
                "contract_address": addr,
            })
    return rows


def upsert_contracts(
    conn: psycopg.Connection,
    contracts: list[dict],
    dry_run: bool,
) -> int:
    """写入 core.asset_contract，冲突时不动（避免覆盖已有映射）。"""
    if not contracts:
        return 0

    if dry_run:
        for c in contracts:
            print(f"  [dry-run] asset_id={c['asset_id']} chain={c['chain']} addr={c['contract_address']}")
        return len(contracts)

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO core.asset_contract
                (asset_id, chain, contract_address, decimals, is_native, is_primary, source_code)
            VALUES
                (%(asset_id)s, %(chain)s, %(contract_address)s, NULL, FALSE, TRUE, 'cmc_backfill')
            ON CONFLICT (chain, contract_address) DO NOTHING
        """, contracts)
        return cur.rowcount


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    client = CMCClient(settings)

    with get_connection(settings.database_url) as conn:
        asset_type = args.asset_type if args.asset_type else None
        assets = fetch_missing_assets(conn, asset_type, args.asset_id, args.limit)
        print(f"[contract backfill] 待补全资产: {len(assets)} 个")
        if not assets:
            print("无需补全")
            return 0

        # 仅处理有 cmc_id 的资产
        asset_id_map: dict[int, int] = {}
        no_cmc = []
        for a in assets:
            cmc_id = a.get("cmc_id")
            if cmc_id:
                asset_id_map[int(cmc_id)] = a["asset_id"]
            else:
                no_cmc.append(a["asset_id"])

        if no_cmc:
            print(f"  跳过 {len(no_cmc)} 个无 cmc_id 资产: {no_cmc[:10]}{'...' if len(no_cmc) > 10 else ''}")

        cmc_ids = list(asset_id_map.keys())
        if not cmc_ids:
            print("没有可调用 CMC API 的资产")
            return 0

        total_inserted = 0
        failed_cmc_ids: list[int] = []
        total_assets_with_contract = 0
        batch_count = (len(cmc_ids) + args.batch_size - 1) // args.batch_size

        for i in range(0, len(cmc_ids), args.batch_size):
            batch = cmc_ids[i:i + args.batch_size]
            batch_no = i // args.batch_size + 1
            print(f"\n[批次 {batch_no}/{batch_count}] {len(batch)} 个 cmc_id")

            try:
                payload = client.get_cryptocurrency_info(batch)
                contracts = parse_contracts(payload, asset_id_map)
                if contracts:
                    asset_ids_in_batch = {c["asset_id"] for c in contracts}
                    total_assets_with_contract += len(asset_ids_in_batch)
                    inserted = upsert_contracts(conn, contracts, args.dry_run)
                    total_inserted += inserted
                    print(f"  解析到 {len(contracts)} 条合约，涉及 {len(asset_ids_in_batch)} 个资产，写入 {inserted} 条")
                else:
                    print("  本批未解析到合约地址")
            except Exception as e:
                print(f"  本批失败: {e}")
                failed_cmc_ids.extend(batch)

            if i + args.batch_size < len(cmc_ids):
                time.sleep(args.sleep)

        print(f"\n[完成] 共处理 {len(cmc_ids)} 个资产，写入 {total_inserted} 条合约，"
              f"覆盖 {total_assets_with_contract} 个资产，失败 {len(failed_cmc_ids)} 个")
        if failed_cmc_ids:
            print(f"  失败 cmc_id 前 20: {failed_cmc_ids[:20]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
