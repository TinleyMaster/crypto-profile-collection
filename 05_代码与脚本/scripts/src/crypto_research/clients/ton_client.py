"""
TON 链上客户端（基于 TON Center API v3，免费免 Key）。

封装 Jetton 转账查询，供大额转账监控使用：
  - get_token_transfers(jetton_master, ...) → 近期 Jetton Transfer 事件

数据源：
  - TON Center v3 Index API: https://toncenter.com/api/v3（免费档可用，jetton/transfers）

返回格式与 EtherscanClient.get_token_transfers 对齐。
"""

from __future__ import annotations

import time
from typing import Any

import requests


DEFAULT_RPS = 1.0  # TON Center 免费档保守速率


class TonClient:
    """TON 链上数据采集客户端。"""

    def __init__(self, api_key: str | None = None, calls_per_second: float = DEFAULT_RPS) -> None:
        self.api_key = (api_key or "").strip() or None
        self.calls_per_second = calls_per_second
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "crypto-research-ton/1.0",
        })
        self._decimals_cache: dict[str, int] = {}

    # ── 基础请求 ────────────────────────────────────────────
    @property
    def _base_url(self) -> str:
        return "https://toncenter.com/api/v3"

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, method: str, params: dict[str, Any] | None = None,
             retries: int = 3) -> dict[str, Any] | None:
        self._rate_limit()
        req_params = params.copy() if params else {}
        # v3 用 X-API-Key 请求头（v2 用 query 参数，v3 已弃用）
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        url = f"{self._base_url}/{method}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=req_params, headers=headers, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                print(f"  [ton] API 请求失败 {method}: {e}")
                return None
        return None

    def _json_rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """JSON-RPC 调用（用于 runGetMethod 等）。"""
        self._rate_limit()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        url = f"{self._base_url}/jsonRPC"
        if self.api_key:
            url += f"?api_key={self.api_key}"
        for attempt in range(3):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    print(f"  [ton] RPC 错误 {method}: {data['error']}")
                    return None
                return data.get("result")
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"  [ton] RPC 失败 {method}: {e}")
                return None
        return None

    # ── 代币元数据 ─────────────────────────────────────────
    def get_token_decimals(self, jetton_master: str) -> int:
        """查询 Jetton 代币精度（decimals），带缓存。

        通过 v3 jetton/masters 返回的 jetton_content.decimals 获取。
        """
        if jetton_master in self._decimals_cache:
            return self._decimals_cache[jetton_master]

        default = 9  # TON Jetton 标准精度
        data = self._get("jetton/masters", {"address": jetton_master, "limit": 1})
        if data and data.get("jetton_masters"):
            content = data["jetton_masters"][0].get("jetton_content") or {}
            try:
                decimals = int(content.get("decimals", default))
                default = decimals
            except (TypeError, ValueError):
                pass
        self._decimals_cache[jetton_master] = default
        return default

    # ── 大额转账 ───────────────────────────────────────────
    def get_token_transfers(
        self, jetton_master: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 0,
    ) -> list[dict]:
        """获取 Jetton 代币近期转账。

        接口与 EtherscanClient.get_token_transfers 对齐，返回字段：
          hash, blockNumber(str=lt), timeStamp(str=unix),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        通过 TON Center v3 jetton/transfers 查询。page=2 时返回空（增量）。
        """
        if page > 1:
            return []

        limit = min(offset, 50)
        decimals = self.get_token_decimals(jetton_master)

        params: dict[str, Any] = {
            "jetton_master": jetton_master,
            "limit": limit,
            "sort": "desc",
        }
        data = self._get("jetton/transfers", params)
        if not data or not data.get("jetton_transfers"):
            return []

        transfers: list[dict] = []
        for t in data["jetton_transfers"]:
            if t.get("transaction_aborted"):
                continue
            source = (t.get("source") or "").strip()
            dest = (t.get("destination") or "").strip()
            if not source or not dest:
                continue
            transfers.append({
                "hash": t.get("transaction_hash", ""),
                "blockNumber": str(t.get("transaction_lt", 0)),
                "timeStamp": str(t.get("transaction_now", 0)),
                "from": source,
                "to": dest,
                "value": str(t.get("amount", 0)),
                "tokenDecimal": str(decimals),
                "contractAddress": jetton_master,
                "tokenName": "",
                "tokenSymbol": "",
            })

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["timeStamp"]), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["timeStamp"]))
        return transfers


def get_ton_client(api_key: str | None = None) -> TonClient:
    """获取 TON 客户端（优先 TON Center API Key，缺 Key 走免费档）。"""
    return TonClient(api_key=api_key)