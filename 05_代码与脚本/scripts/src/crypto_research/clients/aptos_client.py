"""
Aptos 链上客户端（基于 Aptos Indexer GraphQL + 公共全节点兜底）。

封装 Coin 转账查询，供大额转账监控使用：
  - get_token_transfers(coin_type, ...) → 近期 Coin Transfer 事件

数据源：
  - Aptos Indexer GraphQL: https://api.mainnet.aptoslabs.com/v1/graphql（fungible_asset_activities，首选）
  - 备选: https://fullnode.mainnet.aptoslabs.com/v1

返回格式与 EtherscanClient.get_token_transfers 对齐。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import requests


DEFAULT_RPS = 2.0  # Aptos Indexer 保守速率（匿名 IP 限流 40k compute units / 300s）


class AptosClient:
    """Aptos 链上数据采集客户端。"""

    def __init__(self, calls_per_second: float = DEFAULT_RPS) -> None:
        self.calls_per_second = calls_per_second
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "crypto-research-aptos/1.0",
        })
        self._rpc_index = 0
        self._decimals_cache: dict[str, int] = {}
        self._coin_type_cache: dict[str, str] = {}

    # ── RPC 端点 ────────────────────────────────────────────
    @property
    def _rpc_urls(self) -> list[str]:
        return [
            "https://fullnode.mainnet.aptoslabs.com/v1",
            "https://aptos-mainnet.publicnode.com",
        ]

    @property
    def _graphql_url(self) -> str:
        return "https://api.mainnet.aptoslabs.com/v1/graphql"

    @property
    def _current_rpc(self) -> str:
        return self._rpc_urls[self._rpc_index]

    def _next_rpc(self) -> bool:
        if self._rpc_index < len(self._rpc_urls) - 1:
            self._rpc_index += 1
            return True
        return False

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None,
             retries: int = 3) -> dict[str, Any] | None:
        self._rate_limit()
        url = f"{self._current_rpc}{path}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    if attempt == retries - 1:
                        self._next_rpc()
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    if attempt == retries - 1:
                        self._next_rpc()
                    continue
                print(f"  [aptos] API 请求失败 {path}: {e}")
                return None
        return None

    def _graphql(self, query: str, retries: int = 4) -> dict[str, Any] | None:
        """Aptos Indexer GraphQL POST。失败返回 None。"""
        self._rate_limit()
        for attempt in range(retries):
            try:
                resp = self.session.post(self._graphql_url, json={"query": query}, timeout=60)
                if resp.status_code in (408, 429, 502, 503):
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    print(f"  [aptos] GraphQL 错误: {data['errors'][0].get('message', '')[:120]}")
                    return None
                return data
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                print(f"  [aptos] GraphQL 失败: {e}")
                return None
        return None

    # ── 工具方法 ───────────────────────────────────────────
    @staticmethod
    def _is_valid_coin_type(coin_type: str) -> bool:
        """判断是否为完整的 Aptos coin_type 格式：address::module::name。

        数据库中部分 aptos 合约地址只有 0x 地址（缺少 module::struct），
        这种格式无法用于 CoinInfo / CoinStore API 查询。
        """
        if not coin_type:
            return False
        parts = coin_type.split("::")
        return len(parts) >= 3 and all(p for p in parts)

    def _resolve_coin_type(self, raw: str) -> str | None:
        """将纯 0x 地址补全为完整 coin_type。

        asset_contract_map 中部分 aptos 合约地址缺 ::module::struct（如 0x357b0b74...）。
        这里通过 Indexer 按地址前缀匹配真实 asset_type 补全；失败返回 None。
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        if self._is_valid_coin_type(raw):
            return raw
        if raw in self._coin_type_cache:
            return self._coin_type_cache[raw]

        resolved = None
        query = (
            '{ fungible_asset_activities('
            'where: {asset_type: {_like: "%s::%%"}},'
            'distinct_on: asset_type, limit: 1)'
            '{ asset_type } }'
        ) % raw
        data = self._graphql(query)
        if data and "data" in data:
            rows = (data["data"].get("fungible_asset_activities") or [])
            if rows:
                resolved = rows[0].get("asset_type") or None
        self._coin_type_cache[raw] = resolved or ""
        return resolved

    # ── 代币元数据 ─────────────────────────────────────────
    def get_token_decimals(self, coin_type: str) -> int:
        """查询 Aptos Coin 精度（decimals），带缓存。

        默认 8 位（Aptos 标准），通过 coin info API 查询。
        coin_type 格式如 "0x1::aptos_coin::AptosCoin"。
        若 coin_type 不是完整的 address::module::name 格式，直接返回默认 8。
        """
        if coin_type in self._decimals_cache:
            return self._decimals_cache[coin_type]

        coin_type = self._resolve_coin_type(coin_type) or coin_type
        if not self._is_valid_coin_type(coin_type):
            self._decimals_cache[coin_type] = 8
            return 8

        # coin_type 格式：address::module::name
        # CoinInfo 资源在 coin_type 的发布地址账户下
        coin_address = coin_type.split("::")[0]
        data = self._get(f"/accounts/{coin_address}/resource/0x1::coin::CoinInfo<{coin_type}>")
        decimals = 8  # Aptos 默认精度
        if data and isinstance(data, dict):
            decimals = data.get("data", {}).get("decimals", 8)
        self._decimals_cache[coin_type] = int(decimals)
        return int(decimals)

    # ── 大额转账 ───────────────────────────────────────────
    def get_token_transfers(
        self, coin_type: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 0,
    ) -> list[dict]:
        """获取 Aptos Coin 近期转账交易。

        接口与 EtherscanClient.get_token_transfers 对齐，返回字段：
          hash, blockNumber(str=version), timeStamp(str=unix_ms),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        通过 Aptos Indexer GraphQL 的 fungible_asset_activities 查询（首选），
        若 coin_type 不是完整的 address::module::name 格式或查询失败，返回空列表。
        page=2 时返回空（增量）。
        """
        if page > 1:
            return []

        # 纯 0x 地址（缺 ::module::struct）先经 Indexer 补全为完整 coin_type
        coin_type = self._resolve_coin_type(coin_type) or coin_type
        if not self._is_valid_coin_type(coin_type):
            return []

        limit = min(offset, 50)
        decimals = self.get_token_decimals(coin_type)

        # 用 Indexer GraphQL 按 coin_type 精确过滤（性能与命中率远优于全量 /transactions 筛）
        query = (
            '{ fungible_asset_activities('
            'where: {asset_type: {_eq: "%s"}, is_gas_fee: {_eq: false}, is_transaction_success: {_eq: true}},'
            'order_by: {transaction_version: desc}, limit: %d)'
            '{ transaction_version transaction_timestamp asset_type amount owner_address type } }'
        ) % (coin_type, limit)

        data = self._graphql(query)

        # Indexer 不可用（限流/超时）时回退到全节点 /transactions 事件解析
        if not data or "data" not in data:
            return self._get_transfers_fullnode(coin_type, limit, decimals)

        transfers: list[dict] = []
        acts = data["data"].get("fungible_asset_activities") or []
        for a in acts:
            version = str(a.get("transaction_version", 0))
            # transaction_timestamp 形如 "2026-08-27T06:46:32"，转成 unix 毫秒
            ts_ms = 0
            raw_ts = a.get("transaction_timestamp") or ""
            try:
                ts_ms = int(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp() * 1000)
            except (ValueError, TypeError):
                ts_ms = 0

            amount = a.get("amount", 0)
            owner = (a.get("owner_address") or "").strip()
            ev_type = a.get("type") or ""

            # amount 为带符号净变化：Deposit 事件 >0（接收方），Withdraw <0（发送方）
            try:
                amt = int(amount)
            except (TypeError, ValueError):
                amt = 0

            if not owner:
                continue

            if "WithdrawEvent" in ev_type or amt < 0:
                transfers.append({
                    "hash": str(version),
                    "blockNumber": version,
                    "timeStamp": str(ts_ms),
                    "from": owner,
                    "to": "",
                    "value": str(abs(amt)),
                    "tokenDecimal": str(decimals),
                    "contractAddress": coin_type,
                    "tokenName": "",
                    "tokenSymbol": "",
                })
            elif "DepositEvent" in ev_type or amt > 0:
                transfers.append({
                    "hash": str(version),
                    "blockNumber": version,
                    "timeStamp": str(ts_ms),
                    "from": "",
                    "to": owner,
                    "value": str(abs(amt)),
                    "tokenDecimal": str(decimals),
                    "contractAddress": coin_type,
                    "tokenName": "",
                    "tokenSymbol": "",
                })

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["blockNumber"] or 0), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["blockNumber"] or 0))
        return transfers

    def _get_transfers_fullnode(
        self, coin_type: str, limit: int, decimals: int
    ) -> list[dict]:
        """回退方案：全节点 /transactions 事件解析（命中率低，仅作兜底）。"""
        data = self._get("/transactions", {"limit": limit})
        if not data or not isinstance(data, list):
            return []

        transfers: list[dict] = []
        for tx in data:
            tx_hash = tx.get("hash", "")
            version = str(tx.get("version", "0"))
            timestamp = str(tx.get("timestamp", "0"))
            timestamp_us = timestamp.replace(".", "")[:16] if timestamp else "0"

            events = tx.get("events", []) or []
            for ev in events:
                ev_type = ev.get("type", "")
                if "coin::DepositEvent" not in ev_type and "coin::WithdrawEvent" not in ev_type:
                    continue
                if coin_type not in ev_type:
                    continue
                ev_data = ev.get("data", {}) or {}
                amount = ev_data.get("amount", "0")
                account = ev.get("guid", {}).get("account_address", "")

                if not amount:
                    continue

                if "WithdrawEvent" in ev_type:
                    transfers.append({
                        "hash": tx_hash,
                        "blockNumber": version,
                        "timeStamp": timestamp_us,
                        "from": account,
                        "to": "",
                        "value": str(amount),
                        "tokenDecimal": str(decimals),
                        "contractAddress": coin_type,
                        "tokenName": "",
                        "tokenSymbol": "",
                    })
                elif "DepositEvent" in ev_type:
                    transfers.append({
                        "hash": tx_hash,
                        "blockNumber": version,
                        "timeStamp": timestamp_us,
                        "from": "",
                        "to": account,
                        "value": str(amount),
                        "tokenDecimal": str(decimals),
                        "contractAddress": coin_type,
                        "tokenName": "",
                        "tokenSymbol": "",
                    })
        return transfers


def get_aptos_client(api_key: str | None = None) -> AptosClient:
    """获取 Aptos 客户端（公共全节点，免费免 Key）。"""
    return AptosClient()