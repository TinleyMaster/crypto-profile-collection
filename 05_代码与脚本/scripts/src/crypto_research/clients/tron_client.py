"""
Tron 链上客户端（基于 TronGrid API，免费免 Key）。

封装 TRC-20 转账查询，供大额转账监控使用：
  - get_token_transfers(contract, ...) → 近期 TRC-20 Transfer 事件

数据源：
  - TronGrid API: https://api.trongrid.io（免费档 15 req/s，无需 Key）
  - 公共 RPC 兜底: https://api.trongrid.io（同一实例）

返回格式与 EtherscanClient.get_token_transfers 对齐。
"""

from __future__ import annotations

import time
from typing import Any

import requests


DEFAULT_RPS = 5.0  # 保守速率，TronGrid 免费档 15 req/s


class TronClient:
    """Tron 链上数据采集客户端。"""

    def __init__(self, api_key: str | None = None, calls_per_second: float = DEFAULT_RPS) -> None:
        self.api_key = (api_key or "").strip() or None
        self.calls_per_second = calls_per_second
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "crypto-research-tron/1.0",
        })
        if self.api_key:
            self.session.headers["TRON-PRO-API-KEY"] = self.api_key
        self._decimals_cache: dict[str, int] = {}

    # ── 基础请求 ────────────────────────────────────────────
    @property
    def _base_url(self) -> str:
        return "https://api.trongrid.io"

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None,
             retries: int = 3) -> dict[str, Any] | None:
        self._rate_limit()
        url = f"{self._base_url}{path}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                print(f"  [tron] API 请求失败 {path}: {e}")
                return None
        return None

    # ── 代币元数据 ─────────────────────────────────────────
    def get_token_decimals(self, contract_address: str) -> int:
        """查询 TRC-20 代币精度（decimals），带缓存。"""
        if contract_address in self._decimals_cache:
            return self._decimals_cache[contract_address]

        data = self._get(f"/v1/contracts/{contract_address}")
        if data and data.get("data"):
            info = data["data"][0] if isinstance(data["data"], list) else data["data"]
            decimals = info.get("decimals", 0)
            if decimals is not None:
                self._decimals_cache[contract_address] = int(decimals)
                return int(decimals)
        return 6  # TRC-20 默认 6 位精度（多数 USDT 类代币），回退值

    # ── 大额转账 ───────────────────────────────────────────
    def get_token_transfers(
        self, contract_address: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 0,
    ) -> list[dict]:
        """获取 TRC-20 代币近期 Transfer 事件。

        接口与 EtherscanClient.get_token_transfers 对齐，返回字段：
          hash, blockNumber(str), timeStamp(str=unix_ms),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        TronGrid events API 按时间戳分页，无传统 block/page 概念。
        这里取最近 24h 的事件，最多 200 条。page=2 时返回空（增量）。
        """
        if page > 1:
            return []  # 增量模式每次只取最新一页

        limit = min(offset, 200)
        decimals = self.get_token_decimals(contract_address)

        # 取最近 24h 的 Transfer 事件
        min_ts = int(time.time() * 1000) - 24 * 3600 * 1000

        data = self._get(
            f"/v1/contracts/{contract_address}/events",
            params={
                "event_name": "Transfer",
                "min_block_timestamp": min_ts,
                "limit": limit,
                "order_by": "block_timestamp,desc",
            },
        )
        if not data or not data.get("data"):
            return []

        transfers = []
        for ev in data["data"]:
            result = ev.get("result", {})
            tx_id = ev.get("transaction_id", "")
            block_num = str(ev.get("block_number", 0))
            ts = str(ev.get("block_timestamp", 0))

            # Tron Transfer 事件: indexed topics: from, to; data: value
            from_addr = result.get("from") or result.get("0", "")
            to_addr = result.get("to") or result.get("1", "")
            raw_value = result.get("value") or result.get("2", "0")

            if not from_addr or not to_addr:
                continue

            transfers.append({
                "hash": tx_id,
                "blockNumber": block_num,
                "timeStamp": ts,
                "from": from_addr,
                "to": to_addr,
                "value": str(raw_value),
                "tokenDecimal": str(decimals),
                "contractAddress": contract_address,
                "tokenName": "",
                "tokenSymbol": "",
            })

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["timeStamp"]), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["timeStamp"]))
        return transfers


def get_tron_client(api_key: str | None = None) -> TronClient:
    """获取 Tron 客户端（优先 TronGrid API Key，缺 Key 走公共免费档）。"""
    return TronClient(api_key=api_key)