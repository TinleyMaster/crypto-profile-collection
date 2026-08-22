"""
RPC 节点客户端（免 API Key 的 Etherscan 替代方案）。

通过公共 RPC 节点的 eth_getLogs 查询 ERC20 Transfer 事件，
返回格式与 EtherscanClient.get_token_transfers() 兼容，
方便大额转账监控在无 API Key 时自动降级。

公共 RPC 节点（免费，无需注册，已实测可用 2026-08）：
  - ETH: publicnode / 1rpc / drpc / mevblocker（eth_getLogs 正常）
  - BSC: bsc-rpc.publicnode.com（binance.org dataseed 有 getLogs 条数限制）

注意：公共 RPC 要求浏览器 User-Agent，否则返回 403。
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any


# 各链的公共 RPC 节点列表（按优先级，失败自动 fallback）
# 均经实测支持 eth_getLogs（2026-08-22 验证）
PUBLIC_RPC_ENDPOINTS: dict[str, list[str]] = {
    "eth": [
        "https://ethereum.publicnode.com",
        "https://ethereum-rpc.publicnode.com",
        "https://eth.drpc.org",
        "https://rpc.mevblocker.io",
        "https://1rpc.io/eth",
    ],
    "bsc": [
        "https://bsc-rpc.publicnode.com",
    ],
}

# ERC20 Transfer 事件 topic: keccak256("Transfer(address,address,uint256)")
TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# 单次 eth_getLogs 默认查多少区块（首次/无 start_block 时的窗口）。
# 约 100 区块 ≈ 20 分钟，足以在热门代币中命中 ≥10 万美元级大额转账，
# 又不会让公共 RPC 单次返回条数超限。
DEFAULT_BLOCK_RANGE = 100

# 增量扫描（指定 start_block）时允许的最大区块跨度。
# 超过则收窄为最近这么多区块（跳过中间区块），避免公共 RPC 超限报错。
MAX_SCAN_RANGE = 100

# 浏览器请求头（公共 RPC 会拒绝非浏览器 UA，返回 403）
_BROWSER_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class RpcTransferClient:
    """RPC 节点客户端，用于查询 ERC20 Transfer 事件。

    接口设计与 EtherscanClient 对齐，返回的转账记录字段尽量兼容，
    方便 phase_chain_transfer_monitor.py 无缝切换。
    """

    def __init__(self, chain: str, calls_per_second: float = 2.0):
        if chain not in PUBLIC_RPC_ENDPOINTS:
            raise ValueError(f"不支持的链: {chain}，可选: {list(PUBLIC_RPC_ENDPOINTS.keys())}")
        self.chain = chain
        self.endpoints = list(PUBLIC_RPC_ENDPOINTS[chain])
        self._endpoint_idx = 0
        self.min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self._req_id = 0
        # 代币精度缓存：contract -> decimals
        self._decimals_cache: dict[str, int] = {}

    @property
    def current_endpoint(self) -> str:
        return self.endpoints[self._endpoint_idx]

    def _next_endpoint(self) -> bool:
        """切换到下一个 RPC 节点。返回 False 表示没有更多节点了。"""
        if self._endpoint_idx < len(self.endpoints) - 1:
            self._endpoint_idx += 1
            print(f"  [rpc:{self.chain}] 切换 RPC 节点 -> {self.current_endpoint}")
            return True
        return False

    def _json_rpc(self, method: str, params: list[Any]) -> dict[str, Any]:
        """发送 JSON-RPC 请求，带速率限制、重试、节点 fallback。"""
        # 速率限制
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        self._req_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._req_id,
        }).encode()

        # 每个节点最多重试 2 次，失败后切换下一节点，总尝试次数有上限
        max_total_attempts = len(self.endpoints) * 2 + 1
        attempts = 0
        per_endpoint_attempts = 0

        while attempts < max_total_attempts:
            attempts += 1
            endpoint = self.current_endpoint
            try:
                self._last_call = time.time()
                req = urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers=_BROWSER_HEADERS,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                if "error" in data:
                    err_msg = str(data["error"]).lower()
                    # 可重试的错误：速率限制 / 条数超限 / 服务器内部错误
                    retryable = any(
                        k in err_msg
                        for k in ("rate limit", "limit", "too many", "internal", "try again")
                    )
                    if retryable:
                        per_endpoint_attempts += 1
                        if per_endpoint_attempts < 2:
                            time.sleep(1.5)
                            continue
                        # 当前节点连续失败，切换节点并重置计数
                        per_endpoint_attempts = 0
                        if self._next_endpoint():
                            continue
                    return {"error": data["error"]}
                return data.get("result", {})
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                per_endpoint_attempts += 1
                if per_endpoint_attempts < 2:
                    time.sleep(1.5)
                    continue
                per_endpoint_attempts = 0
                if self._next_endpoint():
                    continue
                return {"error": str(e)}

        return {"error": "max retries"}

    def get_block_number(self) -> int:
        """获取最新区块号。"""
        result = self._json_rpc("eth_blockNumber", [])
        if isinstance(result, str) and result.startswith("0x"):
            return int(result, 16)
        return 0

    def get_token_decimals(self, contract_address: str) -> int:
        """查询 ERC20 代币精度（decimals），带缓存。

        USDT/USDC 为 6，多数代币为 18。金额换算必须用真实精度，
        否则大额阈值判断会严重失真。查询失败时回退为 18。
        """
        contract = contract_address.lower()
        if not contract.startswith("0x"):
            contract = "0x" + contract
        if contract in self._decimals_cache:
            return self._decimals_cache[contract]

        # decimals() 函数选择器 = 0x313ce567
        result = self._json_rpc("eth_call", [{
            "to": contract,
            "data": "0x313ce567",
        }, "latest"])
        decimals = 18
        if isinstance(result, str) and result.startswith("0x") and len(result) > 2:
            try:
                decimals = int(result, 16)
                if not (0 <= decimals <= 36):
                    decimals = 18
            except ValueError:
                decimals = 18
        self._decimals_cache[contract] = decimals
        return decimals

    def get_token_transfers(
        self, contract_address: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 0,
    ) -> list[dict]:
        """获取代币转账记录（与 EtherscanClient 接口兼容）。

        注意：RPC 方式不支持原生分页，这里通过区块范围 + 截断模拟。
        sort=desc 时从最新区块往前查，sort=asc 时从 start_block 往后查。
        返回字段尽量与 Etherscan API 对齐：
          hash, blockNumber, timeStamp, from, to, value, tokenDecimal, contractAddress
        """
        latest = self.get_block_number()
        if latest == 0:
            return []

        # 确定查询区块范围
        if end_block and end_block < latest:
            to_block = end_block
        else:
            to_block = latest

        if start_block > 0:
            from_block = start_block
            # 增量窗口过大时收窄，避免公共 RPC eth_getLogs 超限（中间区块会被跳过）
            if to_block - from_block > MAX_SCAN_RANGE:
                print(f"  [rpc:{self.chain}] 扫描窗口过大（{to_block - from_block} 区块），"
                      f"收窄为最近 {MAX_SCAN_RANGE} 个区块")
                from_block = to_block - MAX_SCAN_RANGE
        else:
            # 默认查最近 DEFAULT_BLOCK_RANGE 个区块
            from_block = max(0, to_block - DEFAULT_BLOCK_RANGE)

        # 确保地址格式正确（小写 + 0x 前缀）
        contract = contract_address.lower()
        if not contract.startswith("0x"):
            contract = "0x" + contract

        # 查询真实精度（用于金额换算，避免 USDT/USDC 6 位精度被误当 18 位）
        token_decimals = self.get_token_decimals(contract)

        # 构造 topic（Transfer 事件）
        # topic0 = Transfer 事件签名
        # 不指定 topic1/topic2 = 不过滤 from/to
        params = [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": contract,
            "topics": [TRANSFER_EVENT_TOPIC],
        }]

        result = self._json_rpc("eth_getLogs", params)
        if isinstance(result, dict) and "error" in result:
            print(f"  [rpc:{self.chain}] eth_getLogs 失败: {result['error']}")
            return []
        if not isinstance(result, list):
            return []

        # 解析日志为 Etherscan 兼容格式
        transfers = []
        for log in result:
            try:
                topics = log.get("topics", [])
                if len(topics) < 3:
                    continue
                # topic1 = from (indexed), topic2 = to (indexed)
                # 去掉前导 0 填充（20 字节地址 = 40 hex chars）
                from_addr = "0x" + topics[1][-40:].lower()
                to_addr = "0x" + topics[2][-40:].lower()

                # data = value (uint256, 非 indexed)
                data = log.get("data", "0x")
                value_hex = data[2:] if data.startswith("0x") else data
                value = int(value_hex, 16) if value_hex else 0

                block_num_hex = log.get("blockNumber", "0x0")
                block_num = int(block_num_hex, 16) if block_num_hex.startswith("0x") else 0

                tx_hash = log.get("transactionHash", "")

                transfers.append({
                    "hash": tx_hash,
                    "blockNumber": str(block_num),
                    "timeStamp": "0",  # RPC 日志不含时间戳，需要额外查区块
                    "from": from_addr,
                    "to": to_addr,
                    "value": str(value),
                    "tokenDecimal": str(token_decimals),
                    "contractAddress": contract,
                    "tokenName": "",
                    "tokenSymbol": "",
                })
            except (ValueError, IndexError, KeyError):
                continue

        # 排序
        if sort == "desc":
            transfers.sort(key=lambda x: int(x["blockNumber"]), reverse=True)
        else:
            transfers.sort(key=lambda x: int(x["blockNumber"]))

        # 模拟分页
        if offset > 0:
            start_idx = (page - 1) * offset
            end_idx = start_idx + offset
            transfers = transfers[start_idx:end_idx]

        return transfers


def get_rpc_client(chain: str) -> RpcTransferClient | None:
    """获取指定链的 RPC 客户端（免 API Key）。"""
    if chain in PUBLIC_RPC_ENDPOINTS:
        return RpcTransferClient(chain)
    return None
