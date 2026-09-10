"""地址标签富化器。

大额转账监控的旁路机制：收集本批次无标签的陌生地址，
批量去区块浏览器查标签，写入 onchain_address_label，并回填转账记录。

不阻塞主流程，失败静默，仅记录日志。

用法：
    enricher = LabelEnricher(conn, chain="eth", fetcher=explorer_fetcher)
    enricher.collect(addresses)   # 收集陌生地址
    enricher.flush()              # 批量查询 + 写库 + 回填
"""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


CASE_SENSITIVE_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}

# 单次 enrichment 的最大地址数（防一次爬太多）
MAX_BATCH_SIZE = 50
# 置信度：区块浏览器 HTML 爬取 = medium（单源，且可能有解析误差）
ENRICH_CONFIDENCE = "medium"
# 来源标识
ENRICH_SOURCE = "explorer_html"
# 只存高价值标签类型（避免噪声）
ALLOWED_LABEL_TYPES = {"exchange", "smart_money", "whale", "mev_bot", "market_maker", "dex"}


class LabelEnricher:
    """地址标签旁路富化器。

    工作流：
    1. collect() — 收集疑似陌生地址（内部会和 resolver 缓存比对）
    2. flush() — 批量去区块浏览器查标签 → 写入 onchain_address_label
               → 回填 onchain_transfer_log 的 label 数组列
    """

    def __init__(self, conn, chain: str, fetcher, resolver=None,
                 max_batch_size: int = MAX_BATCH_SIZE,
                 dry_run: bool = False):
        self.conn = conn
        self.chain = chain
        self.fetcher = fetcher
        self.resolver = resolver  # AddressLabelResolver 实例（可选，用于比对）
        self.max_batch_size = max_batch_size
        self.dry_run = dry_run
        self._case_sensitive = chain in CASE_SENSITIVE_CHAINS

        # 待富化的地址集合（去重）
        self._pending: set[str] = set()
        # 已尝试过的地址（避免重复查，即使没查到也不再查）
        self._tried: set[str] = set()

    # ── 公共 API ────────────────────────────────────────────

    def collect(self, addresses: list[str]) -> None:
        """收集待富化的地址。内部去重、过滤已知标签。"""
        for addr in addresses:
            na = self._norm(addr)
            if not na:
                continue
            if na in self._tried:
                continue
            # 如果有 resolver，检查是否已有标签
            if self.resolver:
                info = self.resolver.resolve(na)
                if info and info["types"]:
                    continue  # 已有标签，跳过
            self._pending.add(na)

    def flush(self) -> dict[str, Any]:
        """执行一次批量富化：查询 → 写库 → 回填转账记录。

        返回统计信息：
            {
                "collected": int,     # 本次收集的地址数
                "fetched": int,       # 成功查到标签的地址数
                "inserted": int,      # 新写入标签库的数量
                "backfilled": int,    # 回填了多少条转账记录
                "skipped": int,       # 跳过（无标签/失败）的地址数
            }
        """
        stats = {
            "collected": len(self._pending),
            "fetched": 0,
            "inserted": 0,
            "backfilled": 0,
            "skipped": 0,
        }
        if not self._pending:
            return stats

        # 分批处理
        pending_list = list(self._pending)[: self.max_batch_size]
        self._pending = set(self._pending) - set(pending_list)

        # 1. 批量查询区块浏览器
        try:
            label_map = self.fetcher.fetch_batch(pending_list)
        except Exception as e:
            logger.warning(f"[{self.chain}] LabelEnricher 批量查询失败: {e}")
            self._tried.update(pending_list)
            return stats

        stats["fetched"] = len(label_map)
        stats["skipped"] = len(pending_list) - len(label_map)
        self._tried.update(pending_list)

        if not label_map:
            return stats

        # 2. 写入 onchain_address_label（dry-run 时跳过）
        if not self.dry_run:
            try:
                stats["inserted"] = self._write_labels(label_map)
            except Exception as e:
                logger.warning(f"[{self.chain}] LabelEnricher 写标签库失败: {e}")

        # 3. 回填 onchain_transfer_log（dry-run 时跳过）
        if not self.dry_run:
            try:
                stats["backfilled"] = self._backfill_transfers(list(label_map.keys()))
            except Exception as e:
                logger.warning(f"[{self.chain}] LabelEnricher 回填转账记录失败: {e}")

        # 4. 更新 resolver 缓存（如果有）
        if self.resolver:
            for addr, info in label_map.items():
                self.resolver._cache[addr] = {
                    "types": [info["label_type"]],
                    "names": [info["display_name"]],
                    "is_exchange": info["is_exchange"],
                    "exchange_name": info["display_name"] if info["is_exchange"] else None,
                }
                self.resolver._no_label.discard(addr)

        return stats

    # ── 内部方法 ────────────────────────────────────────────

    def _norm(self, address: str) -> str:
        if not address:
            return ""
        if self._case_sensitive:
            return address.strip()
        return address.strip().lower()

    def _write_labels(self, label_map: dict[str, dict[str, Any]]) -> int:
        """批量写入 onchain_address_label，返回新插入的数量。

        只写入 ALLOWED_LABEL_TYPES 白名单内的标签类型，避免噪声。
        """
        # 过滤白名单
        filtered = {
            addr: info for addr, info in label_map.items()
            if info.get("label_type") in ALLOWED_LABEL_TYPES
        }
        if not filtered:
            return 0

        inserted = 0
        with self.conn.cursor() as cur:
            for addr, info in filtered.items():
                raw_meta = json.dumps({
                    "label_text": info["label_text"],
                    "fetched_from": "address_page",
                }, ensure_ascii=False)
                cur.execute("""
                    INSERT INTO biz.onchain_address_label
                        (address, chain, label_type, label_name, display_name,
                         confidence, source, raw_meta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (address, chain, label_type, label_name) DO NOTHING
                """, (
                    addr, self.chain, info["label_type"],
                    info["display_name"], info["display_name"],
                    ENRICH_CONFIDENCE, ENRICH_SOURCE, raw_meta,
                ))
                if cur.rowcount:
                    inserted += 1

            # 如果是交易所，也写入老表 onchain_exchange_wallet（向后兼容）
            for addr, info in filtered.items():
                if not info["is_exchange"]:
                    continue
                cur.execute("""
                    INSERT INTO biz.onchain_exchange_wallet
                        (address, exchange_name, chain, label, confidence, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (address, chain) DO NOTHING
                """, (
                    addr, info["display_name"], self.chain,
                    info["label_text"], ENRICH_CONFIDENCE, ENRICH_SOURCE,
                ))

        self.conn.commit()
        return inserted

    def _backfill_transfers(self, addresses: list[str]) -> int:
        """回填 onchain_transfer_log 中涉及这些地址的记录的标签列。

        思路：把这些地址重新过一遍 resolver（现在标签库已有了），
        然后批量 update from_labels/to_labels/from_label_names/to_label_names。
        用临时表 + 批量 UPDATE 的方式（和 backfill_transfer_labels.py 同款）。
        """
        if not addresses:
            return 0

        # 如果有 resolver，先刷新缓存
        if self.resolver:
            self.resolver._no_label -= set(addresses)
            self.resolver.resolve_batch(addresses)

        # 用临时表方式批量更新
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE tmp_enrich_labels (
                    address TEXT PRIMARY KEY,
                    label_types TEXT[],
                    label_names TEXT[],
                    is_exchange BOOLEAN DEFAULT false,
                    exchange_name TEXT
                ) ON COMMIT DROP
            """)

            # 用 resolver 查询（如果有），否则重新查 DB
            if self.resolver:
                rows = []
                for addr in addresses:
                    info = self.resolver.resolve(addr)
                    if info["types"]:
                        rows.append((
                            addr, info["types"], info["names"],
                            info["is_exchange"], info.get("exchange_name"),
                        ))
            else:
                # 直接查 onchain_address_label
                cur.execute("""
                    SELECT address,
                           ARRAY_AGG(label_type ORDER BY CASE confidence
                               WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END) as types,
                           ARRAY_AGG(display_name ORDER BY CASE confidence
                               WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END) as names,
                           BOOL_OR(label_type = 'exchange') as is_exchange,
                           MAX(CASE WHEN label_type = 'exchange' THEN display_name END) as exchange_name
                    FROM biz.onchain_address_label
                    WHERE chain = %s AND address = ANY(%s)
                    GROUP BY address
                """, (self.chain, addresses))
                rows = [(r[0], r[1], r[2], r[3], r[4]) for r in cur.fetchall()]

            if rows:
                cur.executemany("""
                    INSERT INTO tmp_enrich_labels (address, label_types, label_names, is_exchange, exchange_name)
                    VALUES (%s, %s, %s, %s, %s)
                """, rows)

            # 更新发件方标签
            cur.execute("""
                UPDATE biz.onchain_transfer_log t
                SET from_labels = e.label_types,
                    from_label_names = e.label_names
                FROM tmp_enrich_labels e
                WHERE t.chain = %s
                  AND LOWER(t.from_address) = LOWER(e.address)
                  AND (t.from_labels IS NULL OR t.from_labels = ARRAY['unknown']::TEXT[])
            """, (self.chain,))
            from_updated = cur.rowcount

            # 更新收件方标签
            cur.execute("""
                UPDATE biz.onchain_transfer_log t
                SET to_labels = e.label_types,
                    to_label_names = e.label_names
                FROM tmp_enrich_labels e
                WHERE t.chain = %s
                  AND LOWER(t.to_address) = LOWER(e.address)
                  AND (t.to_labels IS NULL OR t.to_labels = ARRAY['unknown']::TEXT[])
            """, (self.chain,))
            to_updated = cur.rowcount

        self.conn.commit()
        return from_updated + to_updated
