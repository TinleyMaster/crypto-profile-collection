"""
链上持仓快照批量采集脚本。
遍历所有有合约地址但尚无今日持仓快照的资产，按链分组批量采集。

用法:
    python phase_chain_holder_batch.py --chains bsc,eth,base,arb,solana
    python phase_chain_holder_batch.py --chains bsc --limit 100
    python phase_chain_holder_batch.py --all-chains
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

sys.stdout.reconfigure(line_buffering=True)

import psycopg
import psycopg.rows

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection


# 支持的链（有可靠免费数据源的优先）
SUPPORTED_CHAINS = {
    "bsc": "Binplorer API (免费)",
    "eth": "Ethplorer API (免费)",
    "base": "Blockscout API (免费)",
    "arb": "Blockscout API (免费)",
    "solana": "Solscan 网页解析",
    "polygon": "Polygonscan 网页解析 (可能被CF拦截)",
    "avax": "Snowtrace 网页解析 (可能被CF拦截)",
    "opt": "Optimism Etherscan 网页解析 (可能被CF拦截)",
}

# 链名别名：数据库名 -> 脚本简称
CHAIN_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "bnb": "bsc",
    "binance": "bsc",
    "polygon": "polygon",
    "matic": "polygon",
    "arbitrum": "arb",
    "arb": "arb",
    "optimism": "opt",
    "op": "opt",
    "base": "base",
    "avalanche": "avax",
    "avax": "avax",
    "solana": "solana",
    "sol": "solana",
}


def get_pending_assets(conn, chain_short: str, limit: int) -> list[dict]:
    """获取指定链上有合约地址但尚无今日快照的资产列表。"""
    db_names = tuple(
        k for k, v in CHAIN_ALIASES.items() if v == chain_short
    )
    if not db_names:
        return []

    placeholders = ",".join(["%s"] * len(db_names))

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            SELECT c.asset_id, c.contract_address, c.chain,
                   a.canonical_symbol AS symbol, a.canonical_name AS name
            FROM core.asset_contract c
            JOIN core.asset a ON a.asset_id = c.asset_id
            WHERE c.chain IN ({placeholders})
              AND c.contract_address IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM biz.onchain_holder_snapshot s
                  WHERE s.asset_id = c.asset_id
                    AND s.chain = c.chain
                    AND s.snapshot_date >= CURRENT_DATE
              )
            ORDER BY c.asset_id ASC
            LIMIT %s
            """,
            (*db_names, limit),
        )
        return cur.fetchall()


def get_total_pending(conn, chain_short: str) -> int:
    """获取指定链待采集总数。"""
    db_names = tuple(
        k for k, v in CHAIN_ALIASES.items() if v == chain_short
    )
    if not db_names:
        return 0

    placeholders = ",".join(["%s"] * len(db_names))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM core.asset_contract c
            WHERE c.chain IN ({placeholders})
              AND c.contract_address IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM biz.onchain_holder_snapshot s
                  WHERE s.asset_id = c.asset_id
                    AND s.chain = c.chain
                    AND s.snapshot_date >= CURRENT_DATE
              )
            """,
            db_names,
        )
        return cur.fetchone()[0]


def run_single(asset_id: int, chain: str, timeout: int = 30) -> bool:
    """运行单币持仓快照采集，返回是否成功。"""
    script = SCRIPT_DIR / "phase_chain_holder_scrape.py"
    try:
        result = subprocess.run(
            [
                sys.executable, "-u", str(script),
                "--asset-id", str(asset_id),
                "--chain", chain,
                "--save",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SCRIPT_DIR),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="链上持仓快照批量采集")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--chains", type=str,
                   help="要采集的链，逗号分隔，如 bsc,eth,base,arb,solana")
    g.add_argument("--all-chains", action="store_true", help="采集所有支持的链")
    parser.add_argument("--limit", type=int, default=0,
                        help="每链最多采集数量 (0=不限)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="单币超时时间（秒）")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每币之间延迟（秒，避免触发限流）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)

    if args.all_chains:
        chains = list(SUPPORTED_CHAINS.keys())
    else:
        chains = [c.strip() for c in args.chains.split(",") if c.strip()]
        invalid = [c for c in chains if c not in SUPPORTED_CHAINS]
        if invalid:
            print(f"ERROR: 不支持的链: {invalid}")
            print(f"支持的链: {list(SUPPORTED_CHAINS.keys())}")
            return 1

    print("=" * 60)
    print("链上持仓快照批量采集")
    print(f"目标链: {chains}")
    print(f"每链限制: {args.limit if args.limit > 0 else '不限'}")
    print("=" * 60)

    total_success = 0
    total_fail = 0
    t0 = time.time()

    with get_connection(settings.database_url) as conn:
        for chain in chains:
            total_pending = get_total_pending(conn, chain)
            limit = args.limit if args.limit > 0 else total_pending
            if limit == 0:
                print(f"\n[{chain}] 待采集: 0 (全部完成或无合约)，跳过")
                continue

            print(f"\n[{chain}] 待采集总数: {total_pending}，本次处理: {limit}")
            print(f"  数据源: {SUPPORTED_CHAINS[chain]}")

            assets = get_pending_assets(conn, chain, limit)
            if not assets:
                print(f"  无待采集资产")
                continue

            chain_success = 0
            chain_fail = 0

            for i, asset in enumerate(assets, 1):
                asset_id = asset["asset_id"]
                symbol = asset.get("symbol", "?")
                print(f"  [{i}/{len(assets)}] asset_id={asset_id} {symbol} ... ",
                      end="", flush=True)

                ok = run_single(asset_id, chain, timeout=args.timeout)
                if ok:
                    chain_success += 1
                    print("OK")
                else:
                    chain_fail += 1
                    print("FAIL")

                if i < len(assets) and args.delay > 0:
                    time.sleep(args.delay)

            total_success += chain_success
            total_fail += chain_fail
            print(f"  本链完成: 成功 {chain_success}, 失败 {chain_fail}")

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print(f"全部完成，耗时 {elapsed:.1f}s")
    print(f"总计: 成功 {total_success}, 失败 {total_fail}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
