"""
Ethplorer / Binplorer API 客户端。
免费获取代币持有者列表，无需 API Key。
支持 Ethereum (ethplorer.io) 和 BNB Chain (binplorer.com)。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any


CHAIN_BASE = {
    "eth": "https://api.ethplorer.io",
    "bsc": "https://api.binplorer.com",
}

# 数据库链名 → Ethplorer 链名映射
CHAIN_ALIASES = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "bnb": "bsc",
    "binance": "bsc",
    "binance-smart-chain": "bsc",
}


class EthplorerClient:
    """Ethplorer API 客户端，免费 tier。

    速率限制：freekey 约 50 req/min，30 req/min 安全。
    """

    def __init__(self, chain: str, api_key: str = "freekey", calls_per_second: float = 0.5):
        if chain not in CHAIN_BASE:
            raise ValueError(f"不支持的链: {chain}，可选: {list(CHAIN_BASE.keys())}")
        self.chain = chain
        self.api_key = api_key
        self.base_url = CHAIN_BASE[chain]
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0

    def _get(self, path: str) -> dict[str, Any]:
        """调用 API，带速率限制和重试。"""
        last_error = ""
        for attempt in range(3):
            elapsed = time.time() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

            url = f"{self.base_url}{path}&apiKey={self.api_key}"
            try:
                self._last_call = time.time()
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code in (429, 403):
                    time.sleep(2 ** attempt)  # 指数退避
                    continue
                return {"error": {"code": e.code, "message": last_error}}
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                last_error = str(e)[:100]
                if "ConnectionReset" in last_error or "10054" in last_error:
                    time.sleep(2 ** attempt)  # 限流重试
                    continue
                return {"error": {"code": -1, "message": last_error}}

        return {"error": {"code": -2, "message": f"重试3次后仍然失败: {last_error}"}}

    def get_token_holders(self, contract_address: str, limit: int = 100) -> tuple[list[dict], str]:
        """获取代币 Top 持有者列表（按持仓量降序）。

        返回: (holders_list, error_reason)
          - holders_list: 持有者列表，格式 [{"address": "0x...", "balance": 123.45, "share": 12.3}, ...]
          - error_reason: 空字符串=成功，否则为错误原因
        """
        data = self._get(
            f"/getTopTokenHolders/{contract_address}?limit={limit}"
        )
        if "error" in data:
            err = data["error"]
            return [], f"API错误({err.get('code', '?')}): {err.get('message', '')}"
        holders = data.get("holders", [])
        result = []
        for h in holders:
            result.append({
                "address": h.get("address", ""),
                "balance": float(h.get("balance", 0)),
                "share": float(h.get("share", 0)),
            })
        return result, ""

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

    def get_token_transfers(self, contract_address: str, page: int = 1,
                            offset: int = 100, sort: str = "desc",
                            start_block: int = 0) -> list[dict]:
        """获取代币近期转账列表（getTokenHistory），无需 Etherscan Key。

        这是持仓快照 getTopTokenHolders 的"转账版"等价数据源：免费、免 Key、
        返回真实时间戳与 raw 金额。适用于大额转账监控的主链路（替代失效的
        Etherscan tokentx API）。

        返回字段归一化为与 phase_chain_transfer_monitor.collect_transfers 一致的
        形状：value(raw 字符串) / tokenDecimal / from / to / hash / timeStamp(epoch) /
        blockNumber。

        注意：分页参数名为 limit（非 pageSize），缺省仅返回 10 条；单次最多 1000 条。
        历史窗口约 30 天（免费档）/ 更长（Personal Key）。
        """
        # getTokenHistory 的分页参数叫 limit，不是 pageSize；缺省只给 10 条。
        # 实测 offset 可达 1000，故这里统一取 min(offset, 1000)。
        limit = min(offset, 1000)
        data = self._get(
            f"/getTokenHistory/{contract_address}?limit={limit}&page={page}"
        )
        if "error" in data:
            return []
        ops = data.get("operations") or []
        result = []
        for op in ops:
            # 只保留实际转账（跳过 approval 等无 from/to 的操作）
            if op.get("type") not in ("transfer", "transferFrom", None):
                continue
            tx_hash = op.get("transactionHash") or op.get("hash") or ""
            from_addr = op.get("from") or ""
            to_addr = op.get("to") or ""
            raw_value = op.get("value")
            if not (tx_hash and from_addr and to_addr and raw_value is not None):
                continue
            ti = op.get("tokenInfo") or {}
            try:
                decimals = int(ti.get("decimals", 18))
            except (ValueError, TypeError):
                decimals = 18
            result.append({
                "value": str(raw_value),
                "tokenDecimal": decimals,
                "from": from_addr.lower(),
                "to": to_addr.lower(),
                "hash": tx_hash,
                "timeStamp": str(int(op.get("timestamp", 0) or 0)),
                "blockNumber": "0",
                "sort": sort,
                "start_block": start_block,
            })
        return result


def get_ethplorer_client(chain: str, api_key: str | None = None) -> EthplorerClient | None:
    """获取指定链的 Ethplorer 客户端。支持数据库链名别名。

    api_key 缺省时读取环境变量 BINPLORER_API_KEY（Personal Key 优先，其次 freekey）。
    Personal Key：10 req/s、单次最多 1000 条、1 年历史；
    freekey：2 req/s、单次最多 100 条、仅 30 天历史。
    """
    normalized = CHAIN_ALIASES.get(chain.lower(), chain.lower())
    if normalized not in CHAIN_BASE:
        return None
    if not api_key:
        api_key = os.getenv("BINPLORER_API_KEY", "").strip() or "freekey"
    if normalized == "bsc":
        # Binplorer：Personal Key 10 req/s；freekey 2 req/s
        calls_per_second = 10.0 if api_key != "freekey" else 2.0
    else:
        # Ethplorer 免费 tier：约 50 req/min，保持 30 req/min 保守限速
        calls_per_second = 0.5
    return EthplorerClient(normalized, api_key=api_key, calls_per_second=calls_per_second)