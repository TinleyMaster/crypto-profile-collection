"""EVM 跨链地址传播 — 全量回填脚本。

对所有 EVM 链上已有的 high 级交易所钱包地址，传播到其他 EVM 链，
创建 medium 级别的副本（source=evm_propagate）。

幂等：目标链已有同地址记录时跳过，可重复执行。

用法：
    # 先预览（不写入）
    python backfill_evm_propagate.py --dry-run

    # 实际执行
    python backfill_evm_propagate.py

    # 仅对指定链做回填（如只回填 base 链）
    python backfill_evm_propagate.py --target-chains base
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 确保能 import 到 crypto_research 包
SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import psycopg

from crypto_research.clients.evm_propagate import (
    EVMAddressPropagator, EVM_CHAINS, PROPAGATE_SOURCE,
)


def get_db_url() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql://root:iuU2F8Vx1aj7A6gw3Pd4bH9rG5eL0RyW@43.166.198.83:32405/crypto"
    )


def preview_before(conn, target_chains: set[str]) -> dict:
    """回填前预览：统计将被影响的行数。"""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 1. 现有 high 级 EVM 钱包数量（源数据）
        cur.execute("""
            SELECT chain, COUNT(*) as cnt
            FROM biz.onchain_exchange_wallet
            WHERE chain = ANY(%s) AND confidence = 'high'
            GROUP BY chain ORDER BY chain
        """, (list(EVM_CHAINS),))
        source_by_chain = {r["chain"]: r["cnt"] for r in cur.fetchall()}

        # 2. 目标链上现有 evm_propagate 数量（已有多少）
        cur.execute("""
            SELECT chain, COUNT(*) as cnt
            FROM biz.onchain_exchange_wallet
            WHERE chain = ANY(%s) AND source = %s
            GROUP BY chain ORDER BY chain
        """, (list(target_chains), PROPAGATE_SOURCE))
        existing_prop = {r["chain"]: r["cnt"] for r in cur.fetchall()}

        # 3. 估算：每条源链的 high 地址数 × (目标链数 - 1) - 已有传播数
        #    （粗略估算，实际会因为地址重复而更少）
        total_source = sum(source_by_chain.values())
        n_targets = len(target_chains)
        # 每条源地址最多传播到 n_targets - 1 条目标链
        # 但很多地址在多链上已经有了，所以粗略估算上限
        est_max_new = total_source * max(n_targets - 1, 0)

    return {
        "source_by_chain": source_by_chain,
        "existing_propagate_by_chain": existing_prop,
        "total_source_high": total_source,
        "est_max_new_wallets": est_max_new,
    }


def main():
    parser = argparse.ArgumentParser(
        description="EVM 跨链地址全量传播回填"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览，不实际写入")
    parser.add_argument("--target-chains", default="",
                        help="逗号分隔的目标链列表，默认所有 EVM 链")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("evm_propagate_backfill")

    target_chains = set(EVM_CHAINS)
    if args.target_chains:
        target_chains = {
            c.strip().lower()
            for c in args.target_chains.split(",")
            if c.strip()
        }
        invalid = target_chains - EVM_CHAINS
        if invalid:
            logger.error("无效的链: %s（可用 EVM 链: %s）",
                         invalid, sorted(EVM_CHAINS))
            sys.exit(1)

    db_url = get_db_url()
    conn = psycopg.connect(
        db_url,
        connect_timeout=30,
        options="-c lock_timeout=30000",
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=3,
    )

    try:
        # 预览
        info = preview_before(conn, target_chains)
        logger.info("=== EVM 跨链传播回填预览 ===")
        logger.info("EVM 链列表: %s", sorted(EVM_CHAINS))
        logger.info("目标链: %s", sorted(target_chains))
        logger.info("各链 high 级钱包数:")
        for chain, cnt in sorted(info["source_by_chain"].items()):
            logger.info("  %-12s %s", chain, cnt)
        logger.info("  %-12s %s", "TOTAL", info["total_source_high"])
        logger.info("目标链已有 evm_propagate 钱包数:")
        for chain, cnt in sorted(info["existing_propagate_by_chain"].items()):
            logger.info("  %-12s %s", chain, cnt)
        logger.info("理论最大新增钱包数: %s（实际会因地址已存在而更少）",
                    info["est_max_new_wallets"])

        if args.dry_run:
            logger.info("dry-run 模式，退出。")
            return

        logger.info("=== 开始执行传播 ===")
        prop = EVMAddressPropagator(conn)
        stats = prop.propagate_all_high_exchanges()

        logger.info("=== 完成 ===")
        logger.info("exchange_wallet 新增: %s", stats.wallets_inserted)
        logger.info("address_label 新增: %s", stats.labels_inserted)
        logger.info("跳过（钱包已存在）: %s", stats.skipped_existing_wallet)
        logger.info("跳过（标签已存在）: %s", stats.skipped_existing_label)

        # 最终统计
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT chain, confidence, COUNT(*) as cnt
                FROM biz.onchain_exchange_wallet
                WHERE source = %s
                GROUP BY chain, confidence
                ORDER BY chain, confidence
            """, (PROPAGATE_SOURCE,))
            rows = cur.fetchall()
            logger.info("=== evm_propagate 来源最终统计（exchange_wallet）===")
            for r in rows:
                logger.info("  %-12s %-8s %s", r["chain"], r["confidence"], r["cnt"])

    finally:
        conn.close()


if __name__ == "__main__":
    main()
