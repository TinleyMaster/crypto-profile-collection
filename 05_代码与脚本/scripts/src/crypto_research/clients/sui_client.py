"""
Sui 链上客户端（基于 Sui 公共 RPC，免费免 Key）。

封装 Coin/Token 转账查询，供大额转账监控使用：
  - get_token_transfers(coin_type, ...) → 近期 Coin Transfer 事件

数据源：
  - Sui 公共 RPC: https://fullnode.mainnet.sui.io
  - 备选: https://sui-rpc.publicnode.com

返回格式与 EtherscanClient.get_token_transfers 对齐。
"""

from __future__ import annotations

import time
from typing import Any

import requests


DEFAULT_RPS = 3.0  # Sui 公共 RPC 保守速率


class SuiClient:
    """Sui 链上数据采集客户端。"""

    def __init__(self, calls_per_second: float = DEFAULT_RPS) -> None:
        self.calls_per_second = calls_per_second
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "crypto-research-sui/1.0",
        })
        self._rpc_index = 0
        self._decimals_cache: dict[str, int] = {}
        self._req_id = 0

    # ── RPC 端点 ────────────────────────────────────────────
    @property
    def _rpc_urls(self) -> list[str]:
        return [
            "https://fullnode.mainnet.sui.io",
            "https://sui-rpc.publicnode.com",
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

    def _json_rpc(self, method: str, params: list[Any],
                  retries: int = 3) -> dict[str, Any] | None:
        self._rate_limit()
        self._req_id += 1
        payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
        for attempt in range(retries * len(self._rpc_urls)):
            try:
                resp = self.session.post(self._current_rpc, json=payload, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2 ** (attempt % 3))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err = str(data["error"]).lower()
                    if any(k in err for k in ("rate limit", "too many")):
                        time.sleep(2 ** (attempt % 3))
                        if attempt % 3 == 2:
                            self._next_rpc()
                        continue
                    return None
                return data.get("result")
            except Exception as e:
                if attempt < retries * len(self._rpc_urls) - 1:
                    time.sleep(2 ** (attempt % 3))
                    if attempt % 3 == 2:
                        self._next_rpc()
                    continue
                print(f"  [sui] RPC 失败 {method}: {e}")
                return None
        return None

    # ── 代币元数据 ─────────────────────────────────────────
    def get_token_decimals(self, coin_type: str) -> int:
        """查询 Sui Coin 精度（decimals），带缓存。

        默认 9 位（Sui 标准），通过 sui_getCoinMetadata 查询。
        """
        if coin_type in self._decimals_cache:
            return self._decimals_cache[coin_type]

        result = self._json_rpc("sui_getCoinMetadata", [coin_type])
        decimals = 9  # Sui 默认精度
        if result and isinstance(result, dict):
            decimals = result.get("decimals", 9)
        self._decimals_cache[coin_type] = int(decimals)
        return int(decimals)

    # ── 大额转账 ───────────────────────────────────────────
    def get_token_transfers(
        self, coin_type: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 0,
    ) -> list[dict]:
        """获取 Sui Coin 近期转账交易。

        接口与 EtherscanClient.get_token_transfers 对齐，返回字段：
          hash, blockNumber(str=checkpoint), timeStamp(str=unix_ms),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        通过 sui_queryTransactionBlocks 查询，page=2 时返回空（增量）。
        """
        if page > 1:
            return []

        limit = min(offset, 50)
        decimals = self.get_token_decimals(coin_type)

        # 查询最近的交易块，按 coin type 过滤
        # Sui 没有直接的 "按 coin type 查转账" API，我们用 queryTransactionBlocks
        # 配合 MoveCall 过滤来近似获取
        result = self._json_rpc("sui_queryTransactionBlocks", [{
            "filter": {
                "InputObject": coin_type,
            },
            "options": {
                "showInput": True,
                "showEffects": True,
                "showBalanceChanges": True,
                "showEvents": True,
            },
        }, None, limit, False])  # cursor, limit, descending

        if not result or not isinstance(result, dict):
            return []

        transfers: list[dict] = []
        txs = result.get("data", [])

        for tx in txs:
            digest = tx.get("digest", "")
            checkpoint = tx.get("checkpoint", "0")
            timestamp_ms = tx.get("timestampMs", "0")

            # 从 balanceChanges 中提取与 coin_type 相关的余额变化
            balance_changes = tx.get("balanceChanges", []) or []
            for bc in balance_changes:
                if bc.get("coinType") != coin_type:
                    continue
                owner = bc.get("owner", {}) or {}
                owner_addr = ""
                if isinstance(owner, dict):
                    owner_addr = owner.get("AddressOwner", "")
                amount = bc.get("amount", "0")

                if not owner_addr:
                    continue

                # 注：sui_queryTransactionBlocks 只返回净余额变化，无法区分 from/to
                # 这里取绝对值作为转账金额，from/to 回退到 coin_type 自身
                abs_amount = str(abs(int(amount)))
                transfers.append({
                    "hash": digest,
                    "blockNumber": str(checkpoint),
                    "timeStamp": timestamp_ms,
                    "from": owner_addr if int(amount) < 0 else "",
                    "to": owner_addr if int(amount) > 0 else "",
                    "value": abs_amount,
                    "tokenDecimal": str(decimals),
                    "contractAddress": coin_type,
                    "tokenName": "",
                    "tokenSymbol": "",
                })

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["timeStamp"]), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["timeStamp"]))
        return transfers


def get_sui_client(api_key: str | None = None) -> SuiClient:
    """获取 Sui 客户端（公共 RPC，免费免 Key）。"""
    return SuiClient()