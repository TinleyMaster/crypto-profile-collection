"""地址标签解析器。

从 biz.onchain_address_label 表批量查询地址标签，带内存缓存。
同时兼容老的 onchain_exchange_wallet 表（exchange 标签）。

用法：
    resolver = AddressLabelResolver(conn, chain="eth")
    labels = resolver.resolve("0x1234...")
    # labels = {"types": ["exchange"], "names": ["Binance 5"],
    #           "is_exchange": True, "exchange_name": "Binance"}
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg.rows


CASE_SENSITIVE_CHAINS = {"solana", "tron", "ton", "sui", "aptos"}


class AddressLabelResolver:
    """地址标签解析器（单链，带内存缓存）。

    优先从 onchain_address_label 查，支持多标签；
    onchain_exchange_wallet 作为兜底（exchange 类型）。
    同实例内缓存，避免重复查询。
    """

    def __init__(self, conn, chain: str, min_confidence: str = "medium"):
        self.conn = conn
        self.chain = chain
        self.min_confidence = min_confidence
        self._case_sensitive = chain in CASE_SENSITIVE_CHAINS
        # 缓存: 规范化地址 -> {types: [...], names: [...], is_exchange: bool, exchange_name: str}
        self._cache: dict[str, dict[str, Any]] = {}
        # 已确认无标签的地址（避免重复查）
        self._no_label: set[str] = set()

    # ── 公共 API ────────────────────────────────────────────

    def resolve(self, address: str) -> dict[str, Any]:
        """解析单个地址的标签。

        返回:
            {
                "types": ["exchange", ...],   # 标签类型列表（按置信度排序）
                "names": ["Binance 5", ...],  # 标签名称列表（与 types 一一对应）
                "is_exchange": True/False,    # 是否包含交易所标签
                "exchange_name": "Binance",   # 交易所名（仅当 is_exchange=True）
            }
        """
        addr = self._norm(address)
        if not addr:
            return self._empty()

        if addr in self._cache:
            return self._cache[addr]
        if addr in self._no_label:
            return self._empty()

        # 批量查询时走 resolve_batch 更高效；单个查询也支持
        self.resolve_batch([addr])
        return self._cache.get(addr, self._empty())

    def resolve_batch(self, addresses: list[str]) -> None:
        """批量解析地址标签，结果写入缓存。"""
        # 规范化 + 去重 + 过滤已查过的
        normed = []
        for a in addresses:
            na = self._norm(a)
            if na and na not in self._cache and na not in self._no_label:
                normed.append(na)
        if not normed:
            return

        # 去重
        unique = list(set(normed))

        # 1. 从 onchain_address_label 查
        label_map = self._query_label_table(unique)

        # 2. 从 onchain_exchange_wallet 兜底（exchange 标签）
        #    （address_label 里已有 exchange 的就不用再补了）
        remaining = [a for a in unique if a not in label_map]
        if remaining:
            ex_map = self._query_exchange_table(remaining)
            for addr, ex_name in ex_map.items():
                label_map[addr] = {
                    "types": ["exchange"],
                    "names": [ex_name],
                    "is_exchange": True,
                    "exchange_name": ex_name,
                }

        # 写入缓存
        for addr in unique:
            if addr in label_map:
                self._cache[addr] = label_map[addr]
            else:
                self._no_label.add(addr)

    # ── 内部方法 ────────────────────────────────────────────

    def _empty(self) -> dict[str, Any]:
        return {"types": [], "names": [], "is_exchange": False, "exchange_name": None}

    def _norm(self, address: str) -> str:
        if not address:
            return ""
        if self._case_sensitive:
            return address.strip()
        return address.strip().lower()

    def _conf_rank(self, conf: str) -> int:
        return {"high": 3, "medium": 2, "low": 1}.get(conf, 0)

    def _min_rank(self) -> int:
        return self._conf_rank(self.min_confidence)

    def _query_label_table(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        """从 onchain_address_label 查询，按置信度排序，高置信度在前。"""
        if not addresses:
            return {}

        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT address, label_type, label_name, display_name, confidence
                FROM biz.onchain_address_label
                WHERE chain = %s
                  AND address = ANY(%s)
                  AND confidence IN ('high', 'medium', 'low')
                ORDER BY
                    CASE confidence WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                    label_type
            """, (self.chain, addresses))
            rows = cur.fetchall()

        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            addr = r["address"] if self._case_sensitive else r["address"].lower()
            if addr not in result:
                result[addr] = {
                    "types": [],
                    "names": [],
                    "is_exchange": False,
                    "exchange_name": None,
                }
            label_type = r["label_type"] or "other"
            display = r["display_name"] or r["label_name"] or label_type
            result[addr]["types"].append(label_type)
            result[addr]["names"].append(display)
            if label_type == "exchange":
                result[addr]["is_exchange"] = True
                result[addr]["exchange_name"] = r["label_name"] or display

        return result

    def _query_exchange_table(self, addresses: list[str]) -> dict[str, str]:
        """从 onchain_exchange_wallet 兜底查询交易所名（仅 high 置信度）。"""
        if not addresses:
            return {}

        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("""
                SELECT address, exchange_name
                FROM biz.onchain_exchange_wallet
                WHERE chain = %s
                  AND address = ANY(%s)
                  AND confidence = 'high'
            """, (self.chain, addresses))
            rows = cur.fetchall()

        result = {}
        for r in rows:
            addr = r["address"] if self._case_sensitive else r["address"].lower()
            result[addr] = r["exchange_name"]
        return result
