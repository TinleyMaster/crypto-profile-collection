"""EVM 跨链地址传播器。

利用 EVM 地址格式通用的特性，当某条 EVM 链上的地址被验证为 high 后，
自动在其他 EVM 链上创建 medium 级别的"传播副本"，加速地址标签覆盖。

设计原则：
  1. 仅 EVM 链之间传播（solana/tron/ton 等非 EVM 排除）
  2. 传播副本置信度 = medium，source = evm_propagate，不直接参与净流
  3. 目标链已有同地址记录时跳过（不覆盖任何已有数据）
  4. exchange_wallet 表和 address_label 表（exchange 类型）双表同步传播
  5. display_name / label_name 带来源链标识，便于人工复核

用法：
    from crypto_research.clients.evm_propagate import EVMAddressPropagator
    prop = EVMAddressPropagator(conn)
    stats = prop.propagate_wallet(wallet_id=123)   # 单条传播
    stats = prop.propagate_all_high_exchanges()    # 全量回填
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg.rows


logger = logging.getLogger(__name__)


# EVM 兼容链列表（地址格式通用，0x + 40 hex）
EVM_CHAINS = {
    "eth", "bsc", "polygon", "base", "arbitrum",
    "optimism", "avalanche",
}

# 非 EVM 链（大小写敏感或地址格式不同），排除在传播外
NON_EVM_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}

PROPAGATE_SOURCE = "evm_propagate"
PROPAGATE_CONFIDENCE = "medium"


@dataclass
class PropagationStats:
    """传播统计。"""
    source_chain: str = ""
    source_address: str = ""
    wallets_inserted: int = 0           # exchange_wallet 新增条数
    labels_inserted: int = 0            # address_label 新增条数
    skipped_existing_wallet: int = 0    # 目标链已有钱包，跳过
    skipped_existing_label: int = 0     # 目标链已有标签，跳过
    skipped_non_evm: int = 0            # 非 EVM 链跳过
    target_chains: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"wallet +{self.wallets_inserted} (skip {self.skipped_existing_wallet}), "
            f"label +{self.labels_inserted} (skip {self.skipped_existing_label}), "
            f"targets={self.target_chains}"
        )


class EVMAddressPropagator:
    """EVM 跨链地址传播器。"""

    def __init__(self, conn):
        self.conn = conn

    # ── 公共 API ────────────────────────────────────────────

    def propagate_wallet(self, wallet_id: int) -> PropagationStats:
        """对单个 exchange_wallet 记录做跨链传播。

        通常在人工验证通过（approve）后调用。
        仅当源链是 EVM 且 confidence='high' 时才传播。
        """
        stats = PropagationStats()

        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT wallet_id, address, chain, exchange_name, display_name,
                       confidence, source, label
                FROM biz.onchain_exchange_wallet
                WHERE wallet_id = %s
            """, (wallet_id,))
            row = cur.fetchone()

        if not row:
            logger.warning("propagate_wallet: wallet_id=%s not found", wallet_id)
            return stats

        stats.source_chain = row["chain"]
        stats.source_address = row["address"]

        # 非 EVM 链不传播
        if row["chain"] not in EVM_CHAINS:
            stats.skipped_non_evm = 1
            logger.info("propagate_wallet: chain=%s not EVM, skip", row["chain"])
            return stats

        # 仅 high 置信度才传播（medium 本身就是待验证状态，不能再扩散）
        if row["confidence"] != "high":
            logger.info("propagate_wallet: wallet_id=%s confidence=%s, skip",
                        wallet_id, row["confidence"])
            return stats

        return self._propagate_to_other_evm_chains(
            address=row["address"],
            source_chain=row["chain"],
            exchange_name=row["exchange_name"],
            display_name=row["display_name"],
            label=row["label"] or "exchange",
            stats=stats,
        )

    def propagate_all_high_exchanges(self) -> PropagationStats:
        """全量回填：将所有 EVM 链上的 high 级 exchange 地址传播到其他 EVM 链。

        用纯 SQL INSERT … SELECT + CROSS JOIN 目标链列表，单条语句完成，
        比 Python 逐行循环快 100x+。
        幂等：ON CONFLICT DO NOTHING，可重复执行。
        """
        total_stats = PropagationStats()

        evm_list = sorted(EVM_CHAINS)
        evm_array = list(evm_list)

        # exchange_wallet 批量传播
        # 逻辑：对每条 EVM high 钱包，CROSS JOIN 所有其他 EVM 链，
        #       生成 (address, target_chain) 组合，INSERT 到目标表。
        # display_name 规则："{exchange_name} (propagated from {SOURCE_CHAIN})"
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO biz.onchain_exchange_wallet
                    (address, chain, exchange_name, display_name, label,
                     confidence, source, added_at)
                SELECT DISTINCT ON (w.address, tc.chain)
                       w.address,
                       tc.chain AS chain,
                       w.exchange_name,
                       w.exchange_name || ' (propagated from ' || UPPER(w.chain) || ')'
                           AS display_name,
                       COALESCE(w.label, 'exchange') AS label,
                       %s AS confidence,
                       %s AS source,
                       NOW() AS added_at
                FROM biz.onchain_exchange_wallet w
                CROSS JOIN (
                    SELECT unnest(%s::text[]) AS chain
                ) tc
                WHERE w.chain = ANY(%s)
                  AND w.confidence = 'high'
                  AND tc.chain <> w.chain
                ON CONFLICT (address, chain) DO NOTHING
            """, (PROPAGATE_CONFIDENCE, PROPAGATE_SOURCE,
                  evm_array, evm_array))
            total_stats.wallets_inserted = cur.rowcount

        logger.info("exchange_wallet 传播完成: 新增 %s 条",
                    total_stats.wallets_inserted)

        # address_label 批量传播（仅 exchange 类型）
        # 注：DISTINCT ON 只能是列引用，不能用常量；
        #     由于 label_type 和 label_name 都是派生常量，
        #     真正需要去重的维度是 (address, chain)。
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO biz.onchain_address_label
                    (address, chain, label_type, label_name, display_name,
                     confidence, source, created_at, updated_at)
                SELECT DISTINCT ON (w.address, tc.chain)
                       w.address,
                       tc.chain AS chain,
                       'exchange' AS label_type,
                       w.exchange_name AS label_name,
                       w.exchange_name || ' (propagated from ' || UPPER(w.chain) || ')'
                           AS display_name,
                       %s AS confidence,
                       %s AS source,
                       NOW() AS created_at,
                       NOW() AS updated_at
                FROM biz.onchain_exchange_wallet w
                CROSS JOIN (
                    SELECT unnest(%s::text[]) AS chain
                ) tc
                WHERE w.chain = ANY(%s)
                  AND w.confidence = 'high'
                  AND tc.chain <> w.chain
                ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
            """, (PROPAGATE_CONFIDENCE, PROPAGATE_SOURCE,
                  evm_array, evm_array))
            total_stats.labels_inserted = cur.rowcount

        logger.info("address_label 传播完成: 新增 %s 条",
                    total_stats.labels_inserted)

        # 估算跳过数（总组合数 - 新增数）
        total_pairs = 0
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM biz.onchain_exchange_wallet
                WHERE chain = ANY(%s) AND confidence = 'high'
            """, (evm_array,))
            n_high = cur.fetchone()[0]
            total_pairs = n_high * (len(evm_list) - 1)

        total_stats.skipped_existing_wallet = (
            total_pairs - total_stats.wallets_inserted
        )
        total_stats.skipped_existing_label = (
            total_pairs - total_stats.labels_inserted
        )

        self.conn.commit()
        logger.info("propagate_all_high_exchanges done: %s", total_stats.summary())
        return total_stats

    # ── 内部方法 ────────────────────────────────────────────

    def _propagate_to_other_evm_chains(
        self,
        *,
        address: str,
        source_chain: str,
        exchange_name: str,
        display_name: str | None,
        label: str,
        stats: PropagationStats,
    ) -> PropagationStats:
        """将地址传播到其他所有 EVM 链。"""
        target_chains = [c for c in sorted(EVM_CHAINS) if c != source_chain]
        stats.target_chains = target_chains

        for target_chain in target_chains:
            # 1. 传播到 exchange_wallet
            inserted_w = self._insert_wallet_if_not_exists(
                address=address,
                chain=target_chain,
                exchange_name=exchange_name,
                source_chain=source_chain,
                display_name=display_name,
                label=label,
            )
            if inserted_w:
                stats.wallets_inserted += 1
            else:
                stats.skipped_existing_wallet += 1

            # 2. 传播到 address_label（exchange 类型）
            inserted_l = self._insert_label_if_not_exists(
                address=address,
                chain=target_chain,
                exchange_name=exchange_name,
                source_chain=source_chain,
                display_name=display_name,
            )
            if inserted_l:
                stats.labels_inserted += 1
            else:
                stats.skipped_existing_label += 1

        return stats

    def _insert_wallet_if_not_exists(
        self,
        *,
        address: str,
        chain: str,
        exchange_name: str,
        source_chain: str,
        display_name: str | None,
        label: str,
    ) -> bool:
        """向 exchange_wallet 插入传播副本，已存在则跳过。返回是否插入。"""
        propagated_display = (
            f"{exchange_name} (propagated from {source_chain.upper()})"
        )
        # 如果源 display_name 有内容且不是默认格式，也带上
        if display_name and display_name.strip() and display_name != exchange_name:
            propagated_display = f"{display_name} (from {source_chain.upper()})"

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO biz.onchain_exchange_wallet
                    (address, chain, exchange_name, display_name, label,
                     confidence, source, added_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (address, chain) DO NOTHING
            """, (
                address, chain, exchange_name, propagated_display,
                label, PROPAGATE_CONFIDENCE, PROPAGATE_SOURCE,
            ))
            return cur.rowcount > 0

    def _insert_label_if_not_exists(
        self,
        *,
        address: str,
        chain: str,
        exchange_name: str,
        source_chain: str,
        display_name: str | None,
    ) -> bool:
        """向 address_label 插入传播副本，已存在则跳过。返回是否插入。

        label_type = 'exchange'，label_name = 交易所名，
        display_name 带 propagated 标识。
        """
        propagated_display = (
            f"{exchange_name} (propagated from {source_chain.upper()})"
        )
        if display_name and display_name.strip() and display_name != exchange_name:
            propagated_display = f"{display_name} (from {source_chain.upper()})"

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO biz.onchain_address_label
                    (address, chain, label_type, label_name, display_name,
                     confidence, source, created_at, updated_at)
                VALUES
                    (%s, %s, 'exchange', %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
            """, (
                address, chain, exchange_name, propagated_display,
                PROPAGATE_CONFIDENCE, PROPAGATE_SOURCE,
            ))
            return cur.rowcount > 0
