"""
Ethplorer / Binplorer API 客户端。
免费获取代币持有者列表，无需 API Key。
支持 Ethereum (ethplorer.io) 和 BNB Chain (binplorer.com)。
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any


CHAIN_BASE = {
    "eth": "https://api.ethplorer.io",
    "bsc": "https://api.binplorer.com",
}


class EthplorerClient:
    """Ethplorer API 客户端，免费 tier。"""

    def __init__(self, chain: str, api_key: str = "freekey", calls_per_second: float = 4.5):
        if chain not in CHAIN_BASE:
            raise ValueError(f"不支持的链: {chain}，可选: {list(CHAIN_BASE.keys())}")
        self.chain = chain
        self.api_key = api_key
        self.base_url = CHAIN_BASE[chain]
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0

    def _get(self, path: str) -> dict[str, Any]:
        """调用 API，带速率限制。"""
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        url = f"{self.base_url}{path}&apiKey={self.api_key}"
        try:
            self._last_call = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            return {"error": {"code": -1, "message": str(e)}}

    def get_token_holders(self, contract_address: str, limit: int = 100) -> list[dict]:
        """获取代币 Top 持有者列表（按持仓量降序）。

        返回格式与 Etherscan tokenholderlist 兼容：
        [{"address": "0x...", "balance": 123.45, "share": 12.3}, ...]
        """
        data = self._get(
            f"/getTopTokenHolders/{contract_address}?limit={limit}"
        )
        if "error" in data:
            return []
        holders = data.get("holders", [])
        result = []
        for h in holders:
            result.append({
                "address": h.get("address", ""),
                "balance": float(h.get("balance", 0)),
                "share": float(h.get("share", 0)),
            })
        return result

    def get_token_info(self, contract_address: str) -> dict | None:
        """获取代币基本信息（总供应量、持有者数量等）。"""
        data = self._get(
            f"/getTokenInfo/{contract_address}?"
        )
        if "error" in data:
            return None
        return {
            "address": data.get("address", ""),
            "name": data.get("name", ""),
            "symbol": data.get("symbol", ""),
            "decimals": int(data.get("decimals", 18)),
            "total_supply": float(data.get("totalSupply", 0)),
            "holders_count": int(data.get("holdersCount", 0)),
            "price": data.get("price", {}),
        }


def get_ethplorer_client(chain: str) -> EthplorerClient | None:
    """获取指定链的 Ethplorer 客户端。"""
    if chain not in CHAIN_BASE:
        return None
    return EthplorerClient(chain)