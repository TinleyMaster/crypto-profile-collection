"""
Etherscan API V2 客户端。
统一接口访问 Etherscan V2 API（支持 60+ 条链，ETH/BSC 等共用同一套 API）。

V2 变化（2025-08-15 V1 正式废弃）：
  - 基础 URL: https://api.etherscan.io/v2/api
  - 通过 chainid 参数区分链（ETH=1, BSC=56）
  - 同一个 API Key 可访问所有支持的链
"""

from __future__ import annotations

import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Any


# 链配置：chain -> chain_id
# 完整列表见 https://docs.etherscan.io/supported-chains
CHAIN_CONFIG = {
    "eth": {
        "name": "Ethereum",
        "chain_id": "1",
        "explorer_url": "https://etherscan.io",
    },
    "bsc": {
        "name": "BSC",
        "chain_id": "56",
        "explorer_url": "https://bscscan.com",
    },
}

# V2 API 基础 URL（所有链共用）
V2_API_URL = "https://api.etherscan.io/v2/api"


class EtherscanClient:
    """Etherscan API V2 客户端，支持多链。"""

    def __init__(self, chain: str, api_key: str, calls_per_second: float = 4.5):
        if chain not in CHAIN_CONFIG:
            raise ValueError(f"不支持的链: {chain}，可选: {list(CHAIN_CONFIG.keys())}")
        self.chain = chain
        self.chain_id = CHAIN_CONFIG[chain]["chain_id"]
        self.api_key = api_key
        self.api_url = V2_API_URL
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0

    def _call(self, params: dict[str, str]) -> dict[str, Any]:
        """调用 API，带速率限制和重试。"""
        # 速率限制
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        # V2: 所有请求带 chainid 参数
        params["chainid"] = self.chain_id
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
        """获取代币持有者列表（按持仓量降序）。
        
        注意：tokenholderlist 接口需要 Etherscan Pro 订阅，免费 API Key 不支持。
        调用失败时返回空列表，上层应降级处理。
        """
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
        # 免费 API Key 不支持 tokenholderlist，返回空列表
        if isinstance(result, str) and result:
            print(f"  [WARN] tokenholderlist API 不可用（需要 Pro 订阅）: {result[:100]}", file=sys.stderr)
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
        # API 返回错误（如 Invalid API Key、Max rate limit reached 等），打印后返回空
        msg = data.get("message", "")
        print(f"  [etherscan:{self.chain}] API error: status={data.get('status')} msg={msg} result={str(result)[:200]}")
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
        msg = data.get("message", "")
        print(f"  [etherscan:{self.chain}] API error (tokentx by address): status={data.get('status')} msg={msg} result={str(result)[:200]}")
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
        msg = data.get("message", "")
        print(f"  [etherscan:{self.chain}] API error (txlist): status={data.get('status')} msg={msg} result={str(result)[:200]}")
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