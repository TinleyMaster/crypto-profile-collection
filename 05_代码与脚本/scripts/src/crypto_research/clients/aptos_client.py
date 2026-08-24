"""
Aptos 链上客户端（基于 Aptos 公共全节点 API，免费免 Key）。

封装 Coin 转账查询，供大额转账监控使用：
  - get_token_transfers(coin_type, ...) → 近期 Coin Transfer 事件

数据源：
  - Aptos 公共全节点: https://fullnode.mainnet.aptoslabs.com/v1
  - 备选: https://aptos-mainnet.publicnode.com

返回格式与 EtherscanClient.get_token_transfers 对齐。
"""

from __future__ import annotations

import time
from typing import Any

import requests


DEFAULT_RPS = 3.0  # Aptos 公共 API 保守速率


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

    # ── RPC 端点 ────────────────────────────────────────────
    @property
    def _rpc_urls(self) -> list[str]:
        return [
            "https://fullnode.mainnet.aptoslabs.com/v1",
            "https://aptos-mainnet.publicnode.com",
        ]

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

    # ── 代币元数据 ─────────────────────────────────────────
    def get_token_decimals(self, coin_type: str) -> int:
        """查询 Aptos Coin 精度（decimals），带缓存。

        默认 8 位（Aptos 标准），通过 coin info API 查询。
        coin_type 格式如 "0x1::aptos_coin::AptosCoin"。
        """
        if coin_type in self._decimals_cache:
            return self._decimals_cache[coin_type]

        data = self._get(f"/accounts/{coin_type}/resource/0x1::coin::CoinInfo<{coin_type}>")
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
          hash, blockNumber(str=version), timeStamp(str=unix_us),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        通过 Aptos 全节点 API 查询 Coin 转账事件。
        page=2 时返回空（增量）。
        """
        if page > 1:
            return []

        limit = min(offset, 50)
        decimals = self.get_token_decimals(coin_type)

        # Aptos 的 Coin 转账事件句柄: 0x1::coin::CoinStore<CoinType>
        # 转账事件类型: 0x1::coin::WithdrawEvent + 0x1::coin::DepositEvent
        # 这里用事件 API 查询，获取最近的 deposit/withdraw 事件
        transfers: list[dict] = []

        # 方法：查询最近的交易版本，从中提取 coin 转账
        # 使用 /transactions?limit=N 然后过滤
        data = self._get("/transactions", {
            "limit": limit,
            "start": max(0, start_block - 1) if start_block > 0 else None,
        })
        if not data or not isinstance(data, list):
            return []

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

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["blockNumber"]), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["blockNumber"]))
        return transfers


def get_aptos_client(api_key: str | None = None) -> AptosClient:
    """获取 Aptos 客户端（公共全节点，免费免 Key）。"""
    return AptosClient()