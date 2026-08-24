"""
TON 链上客户端（基于 TON Center API，免费免 Key）。

封装 Jetton 转账查询，供大额转账监控使用：
  - get_token_transfers(jetton_master, ...) → 近期 Jetton Transfer 事件

数据源：
  - TON Center API: https://toncenter.com/api/v2（免费档 1 req/s，可申请 Key 提速）
  - 公共 RPC: https://toncenter.com/api/v2/jsonRPC

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
        return "https://toncenter.com/api/v2"

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, method: str, params: dict[str, Any] | None = None,
             retries: int = 3) -> dict[str, Any] | None:
        self._rate_limit()
        req_params = params.copy() if params else {}
        if self.api_key:
            req_params["api_key"] = self.api_key
        url = f"{self._base_url}/{method}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=req_params, timeout=30)
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

        通过 runGetMethod 调用 jetton master 合约的 get_jetton_data() 方法。
        """
        if jetton_master in self._decimals_cache:
            return self._decimals_cache[jetton_master]

        # get_jetton_data() 返回 (total_supply, mintable, admin_address, content, wallet_code)
        # 我们需要从 content 中解析 decimals，但这里简化：默认 9 位（TON 标准）
        # 实际上 decimals 存储在 jetton content 的 on-chain metadata 中，离线解析成本高
        result = self._json_rpc("runGetMethod", {
            "address": jetton_master,
            "method": "get_jetton_data",
            "stack": [],
        })
        default = 9  # TON Jetton 标准精度
        if result and result.get("stack") and len(result["stack"]) > 0:
            # 总供应量在第一个返回值，格式为 ["num", "0x..."]
            # decimals 不直接返回，回退到默认值
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
          hash, blockNumber(str=seqno), timeStamp(str=utime),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        通过查询 jetton wallet 的近期交易实现。page=2 时返回空（增量）。
        """
        if page > 1:
            return []

        limit = min(offset, 50)
        decimals = self.get_token_decimals(jetton_master)

        transfers: list[dict] = []

        # 获取 jetton master 的近期交易
        data = self._get("getTransactions", {
            "address": jetton_master,
            "limit": limit,
            "archival": "false",
        })
        if not data or not data.get("result"):
            return []

        for tx in data["result"]:
            tx_hash = tx.get("transaction_id", {}).get("hash", "")
            utime = tx.get("utime", 0)
            lt = tx.get("transaction_id", {}).get("lt", "0")
            in_msg = tx.get("in_msg", {}) or {}

            # Jetton Transfer 通知（internal_transfer 或 transfer_notification）
            # 在 out_msgs 中查找 jetton transfer
            out_msgs = tx.get("out_msgs", []) or []
            for msg in out_msgs:
                msg_data = msg.get("msg_data", {}) or {}
                body = msg.get("body") or msg_data.get("body") or ""
                if not body or "transfer" not in body.lower():
                    continue

                # 尝试解析 jetton transfer 消息体
                source = (msg.get("source") or "").lower()
                dest = (msg.get("destination") or "").lower()
                value_raw = msg.get("value", "0")

                if source and dest:
                    transfers.append({
                        "hash": tx_hash,
                        "blockNumber": str(lt),
                        "timeStamp": str(utime),
                        "from": source,
                        "to": dest,
                        "value": str(value_raw),
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