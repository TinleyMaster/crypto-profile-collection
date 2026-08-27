"""
Sui 链上客户端（基于 Sui 公共 RPC，免费免 Key）。

封装 Coin/Token 转账查询，供大额转账监控使用：
  - get_token_transfers(coin_type, ...) → 近期 Coin Transfer 事件

数据源：
  - Sui 公共 RPC: https://sui-rpc.publicnode.com（JSON-RPC 仍可用）
  - fullnode.mainnet.sui.io 已废弃 JSON-RPC，仅作最后兜底

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
        # fullnode.mainnet.sui.io 已废弃 JSON-RPC，publicnode 为首选
        return [
            "https://sui-rpc.publicnode.com",
            "https://fullnode.mainnet.sui.io",
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

        默认 9 位（Sui 标准），通过 suix_getCoinMetadata 查询。
        """
        if coin_type in self._decimals_cache:
            return self._decimals_cache[coin_type]

        result = self._json_rpc("suix_getCoinMetadata", [coin_type])
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

        实现：suix_queryTransactionBlocks 按 MoveModule 0x2::coin 过滤，
        逐个拉取完整交易块并解析 balanceChanges 中该 coin 的余额变化。
        JSON-RPC 已在公共 fullnode 废弃，公共 RPC 仍可用 suix_ 方法。
        """
        if page > 1:
            return []

        limit = min(offset, 50)
        decimals = self.get_token_decimals(coin_type)

        # 查询最近涉及 coin 模块（0x2::coin 传输操作）的交易
        result = self._json_rpc("suix_queryTransactionBlocks", [{
            "filter": {
                "MoveFunction": {"package": "0x2", "module": "coin", "function": "transfer"},
            },
            "options": {
                "showInput": True,
                "showEffects": True,
                "showBalanceChanges": True,
                "showEvents": True,
            },
        }, None, limit, True])

        if not result or not isinstance(result, dict):
            return []

        transfers: list[dict] = []
        txs = result.get("data", [])

        for tx in txs:
            digest = tx.get("digest", "")
            # 列表查询通常只返回 digest，需逐个拉取完整交易块
            detail = self._json_rpc("sui_getTransactionBlock", [
                digest, {
                    "showInput": True,
                    "showEffects": True,
                    "showBalanceChanges": True,
                    "showEvents": True,
                },
            ])
            if not detail or not isinstance(detail, dict):
                continue
            checkpoint = str(detail.get("checkpoint", "0"))
            timestamp_ms = str(detail.get("timestampMs", "0"))

            # 从 balanceChanges 中提取该 coin 的余额变化
            balance_changes = detail.get("balanceChanges", []) or []
            coin_bcs = [bc for bc in balance_changes if bc.get("coinType") == coin_type]
            if not coin_bcs:
                continue

            # 净余额变化：正数 = 接收（to），负数 = 转出（from）
            from_addr = ""
            to_addr = ""
            total_in = 0
            total_out = 0
            for bc in coin_bcs:
                owner = bc.get("owner", {}) or {}
                owner_addr = ""
                if isinstance(owner, dict):
                    owner_addr = owner.get("AddressOwner", "")
                amount = bc.get("amount", "0")
                try:
                    amt = int(amount)
                except (TypeError, ValueError):
                    amt = 0
                if not owner_addr:
                    continue
                if amt < 0:
                    from_addr = owner_addr
                    total_out += -amt
                elif amt > 0:
                    to_addr = owner_addr
                    total_in += amt

            # 取较大侧作为转账金额（双边转账时 from/to 分别来自不同 owner）
            value = max(total_in, total_out) or abs(sum(int(bc.get("amount", 0)) for bc in coin_bcs))

            transfers.append({
                "hash": digest,
                "blockNumber": checkpoint,
                "timeStamp": timestamp_ms,
                "from": from_addr,
                "to": to_addr,
                "value": str(value),
                "tokenDecimal": str(decimals),
                "contractAddress": coin_type,
                "tokenName": "",
                "tokenSymbol": "",
            })

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["timeStamp"] or 0), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["timeStamp"] or 0))
        return transfers


def get_sui_client(api_key: str | None = None) -> SuiClient:
    """获取 Sui 客户端（公共 RPC，免费免 Key）。"""
    return SuiClient()