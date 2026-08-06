"""
Etherscan / BSCScan API 客户端。
统一接口访问 Etherscan 和 BSCScan 的免费 API。
"""

from __future__ import annotations

import time
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Any


# 链配置
CHAIN_CONFIG = {
    "eth": {
        "name": "Ethereum",
        "api_url": "https://api.etherscan.io/api",
        "explorer_url": "https://etherscan.io",
    },
    "bsc": {
        "name": "BSC",
        "api_url": "https://api.bscscan.com/api",
        "explorer_url": "https://bscscan.com",
    },
}


class EtherscanClient:
    """Etherscan / BSCScan API 客户端，支持免费 API 调用。"""

    def __init__(self, chain: str, api_key: str, calls_per_second: float = 4.5):
        if chain not in CHAIN_CONFIG:
            raise ValueError(f"不支持的链: {chain}，可选: {list(CHAIN_CONFIG.keys())}")
        self.chain = chain
        self.api_key = api_key
        self.api_url = CHAIN_CONFIG[chain]["api_url"]
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0

    def _call(self, params: dict[str, str]) -> dict[str, Any]:
        """调用 API，带速率限制和重试。"""
        # 速率限制
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        params["apikey"] = self.api_key
        query_string = urllib.parse.urlencode(params)
        url = f"{self.api_url}?{query_string}"

        for attempt in range(3):
            try:
                self._last_call = time.time()
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return {"status": "0", "message": "ERROR", "result": str(e)}

            if data.get("status") == "1":
                return data
            else:
                msg = data.get("message", "NOTOK")
                result = data.get("result", "")
                # 速率限制时等待重试
                if "rate limit" in str(msg).lower() or "max rate" in str(result).lower():
                    if attempt < 2:
                        time.sleep(3)
                        continue
                return {"status": "0", "message": msg, "result": result}

        return {"status": "0", "message": "ERROR", "result": "max retries"}

    # ── Token 相关 ──

    def get_token_holders(self, contract_address: str, page: int = 1, offset: int = 100) -> list[dict]:
        """获取代币持有者列表（按持仓量降序）。"""
        data = self._call({
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": str(page),
            "offset": str(offset),
        })
        result = data.get("result", [])
        if isinstance(result, list):
            return result
        return []

    def get_token_holder_count(self, contract_address: str) -> int:
        """获取代币持有者总数。"""
        data = self._call({
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": "1",
            "offset": "1",
        })
        # 返回结果中无直接 count，用 totalSupply 替代方案
        # 先获取总供应量来估算
        supply_data = self._call({
            "module": "stats",
            "action": "tokensupply",
            "contractaddress": contract_address,
        })
        return 0  # 免费 API 不直接提供 holder count

    def get_token_transfers(
        self, contract_address: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 99999999,
    ) -> list[dict]:
        """获取代币转账记录。"""
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract_address,
            "page": str(page),
            "offset": str(offset),
            "sort": sort,
            "startblock": str(start_block),
            "endblock": str(end_block),
        }
        data = self._call(params)
        result = data.get("result", [])
        if isinstance(result, list):
            return result
        return []

    def get_token_transfers_by_address(
        self, contract_address: str, address: str, page: int = 1, offset: int = 100,
        sort: str = "desc",
    ) -> list[dict]:
        """获取指定地址的代币转账记录。"""
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract_address,
            "address": address,
            "page": str(page),
            "offset": str(offset),
            "sort": sort,
        }
        data = self._call(params)
        result = data.get("result", [])
        if isinstance(result, list):
            return result
        return []

    def get_account_token_balance(self, contract_address: str, address: str) -> str:
        """查询指定地址的代币余额。"""
        data = self._call({
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": address,
            "tag": "latest",
        })
        return data.get("result", "0")

    # ── 账户相关 ──

    def get_transactions(self, address: str, page: int = 1, offset: int = 100) -> list[dict]:
        """获取地址的普通交易列表。"""
        data = self._call({
            "module": "account",
            "action": "txlist",
            "address": address,
            "page": str(page),
            "offset": str(offset),
            "sort": "desc",
        })
        result = data.get("result", [])
        if isinstance(result, list):
            return result
        return []

    def get_last_active(self, address: str) -> int | None:
        """获取地址最后活跃的区块号（用于判断休眠钱包）。"""
        txs = self.get_transactions(address, page=1, offset=1)
        if txs and len(txs) > 0:
            return int(txs[0].get("blockNumber", 0))
        return None


def get_client(chain: str, api_key: str | None = None) -> EtherscanClient | None:
    """获取指定链的 API 客户端。"""
    if chain == "eth":
        import os
        key = api_key or os.getenv("ETHERSCAN_API_KEY", "")
    elif chain == "bsc":
        import os
        key = api_key or os.getenv("BSCSCAN_API_KEY", "")
    else:
        return None

    if not key:
        return None

    return EtherscanClient(chain, key)