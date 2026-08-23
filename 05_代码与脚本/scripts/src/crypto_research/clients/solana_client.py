"""
Solana 链上客户端（基于 Helius RPC，兼容公共 RPC 兜底）。

封装 Solana JSON-RPC 方法，供持仓快照与大额转账监控使用：
  - getTokenLargestAccounts(mint)  -> Top 20 最大 SPL 持币账户（持仓集中度）
  - getTokenSupply(mint)           -> 总供应量与 decimals（用于 pct 与金额换算）
  - getSignaturesForAddress(mint)  -> 近期涉及该 mint 的签名（转账监控入口）
  - getTransaction(sig)            -> jsonParsed 解析 SPL 转账
  - getAccountInfo(tokenAccount)   -> 反查 token account 的 owner 钱包地址

数据源优先级：
  - 有 HELIUS_API_KEY：走 Helius RPC（免费档足够，速率稳定，getTokenLargestAccounts 不 429）
  - 无 Key：回退公共 RPC api.mainnet-beta.solana.com
    （getTokenLargestAccounts 严重限流，仅作兜底，规模化不可靠）
"""

from __future__ import annotations

import time
from typing import Any

import requests


# Helius 免费档保守速率（实际支持更高，留余量避免突发 429）
DEFAULT_RPS = 5.0


class SolanaClient:
    """Solana 链上数据采集客户端。"""

    def __init__(self, api_key: str | None = None, calls_per_second: float = DEFAULT_RPS) -> None:
        self.api_key = (api_key or "").strip() or None
        self.calls_per_second = calls_per_second
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "crypto-research-solana/1.0",
        })
        self._decimals_cache: dict[str, int] = {}
        self._supply_cache: dict[str, float] = {}
        # 转账签名分页游标（按 mint 隔离），配合 phase_chain_transfer_monitor 的多页循环
        self._sig_cursor: dict[str, str] = {}

    # ── 基础 RPC ────────────────────────────────────────────
    @property
    def _rpc_url(self) -> str:
        if self.api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        return "https://api.mainnet-beta.solana.com"

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _json_rpc(self, method: str, params: list[Any], retries: int = 3) -> dict[str, Any] | None:
        self._rate_limit()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(retries):
            try:
                resp = self.session.post(self._rpc_url, json=payload, timeout=30)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                print(f"  [solana] RPC {method} 失败: {e}")
                return None
            if "error" in data:
                print(f"  [solana] RPC {method} 错误: {data['error']}")
                return None
            return data.get("result")
        return None

    # ── 代币元数据 ─────────────────────────────────────────
    def get_token_supply(self, mint: str) -> dict[str, Any] | None:
        """返回 {decimals, ui_amount, total}，带缓存。"""
        if mint in self._supply_cache:
            return {
                "decimals": self._decimals_cache.get(mint, 0),
                "ui_amount": None,
                "total": self._supply_cache[mint],
            }
        result = self._json_rpc("getTokenSupply", [mint])
        if not result:
            return None
        value = result.get("value", {})
        decimals = int(value.get("decimals", 0))
        amount = float(value.get("amount", 0) or 0)
        total = amount / (10 ** decimals) if decimals else amount
        self._decimals_cache[mint] = decimals
        self._supply_cache[mint] = total
        return {"decimals": decimals, "ui_amount": value.get("uiAmount"), "total": total}

    def get_token_decimals(self, mint: str) -> int:
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        supply = self.get_token_supply(mint)
        return supply["decimals"] if supply else 0

    def _get_owner(self, token_account: str) -> str:
        """反查 SPL token account 的 owner 钱包地址。"""
        result = self._json_rpc("getAccountInfo", [token_account, {"encoding": "jsonParsed"}])
        if not result or not result.get("value"):
            return ""
        data = result["value"].get("data", {})
        parsed = data.get("parsed", {}) if isinstance(data, dict) else {}
        return parsed.get("info", {}).get("owner", "") if isinstance(parsed, dict) else ""

    # ── 持仓（Top 20）──────────────────────────────────────
    def get_token_holders(self, mint: str, limit: int = 20) -> dict[str, Any]:
        """获取 SPL 代币的 Top N 最大持币账户（Helius 免费档上限 20）。

        返回结构对齐 phase_chain_holder_scrape 的 top_holders_json：
          {"rank", "address": owner钱包, "label": "", "amount": 字符串, "pct": float}
        附带 total_supply / decimals / top_N 集中度（按 pct 累加）。
        注：公共 RPC / Helius 免费档无法直接获取 total_holders 总地址数，置 None。
        """
        supply = self.get_token_supply(mint)
        decimals = supply["decimals"] if supply else 0
        total = supply["total"] if supply else 0

        result = self._json_rpc("getTokenLargestAccounts", [mint])
        accounts = (result or {}).get("value", []) if isinstance(result, dict) else []
        accounts = accounts[:limit]

        holders = []
        for i, acc in enumerate(accounts):
            token_account = acc.get("address", "")
            raw_amount = int(acc.get("amount", 0) or 0)
            ui_amount = raw_amount / (10 ** decimals) if decimals else raw_amount
            owner = self._get_owner(token_account)
            pct = (ui_amount / total * 100) if total else 0.0
            holders.append({
                "rank": i + 1,
                "address": owner or token_account,
                "label": "",
                "amount": str(ui_amount),
                "pct": round(pct, 4),
            })

        pcts = [h["pct"] for h in holders]

        def _cum(n: int) -> float | None:
            return round(sum(pcts[:n]), 2) if len(pcts) >= n else None

        return {
            "chain": "solana",
            "contract_address": mint,
            "total_holders": None,
            "total_supply": total if total else None,
            "top_holders_json": holders,
            "tier_distribution_json": [],
            "top_5_pct": _cum(5),
            "top_10_pct": _cum(10),
            "top_25_pct": _cum(25),
            "top_50_pct": _cum(50),
            "top_100_pct": _cum(100),
            "price_usd": None,
            "market_cap_usd": None,
            "source": "helius_rpc",
        }

    # ── 大额转账 ───────────────────────────────────────────
    def get_token_transfers(
        self, mint: str, page: int = 1, offset: int = 100,
        sort: str = "desc", start_block: int = 0, end_block: int = 0,
    ) -> list[dict]:
        """获取涉及该 mint 的近期 SPL 转账。

        接口与 EtherscanClient.get_token_transfers 对齐，返回字段：
          hash, blockNumber(str=slot), timeStamp(str=unix),
          from, to, value(str=base units), tokenDecimal(str),
          contractAddress, tokenName, tokenSymbol

        说明：Solana 增量基于签名游标而非 slot，且无 Helius 专属接口时逐个
        getTransaction 成本较高，故每资产每次只取最新一页（page=1）；
        page>1 直接返回空，由调用方依赖 DB 主键去重实现幂等。
        """
        if page > 1:
            return []

        decimals = self.get_token_decimals(mint)
        limit = min(offset, 50)
        params: list[Any] = [mint, {"limit": limit}]

        sigs = self._json_rpc("getSignaturesForAddress", params)
        if not isinstance(sigs, list) or not sigs:
            return []

        transfers = []
        for sig_entry in sigs:
            sig = sig_entry.get("signature")
            if not sig or sig_entry.get("err"):
                continue
            transfers.extend(self._parse_transaction(sig, mint, decimals))

        if sort == "desc":
            transfers.sort(key=lambda x: int(x["blockNumber"]), reverse=True)
        elif sort == "asc":
            transfers.sort(key=lambda x: int(x["blockNumber"]))
        return transfers

    def _parse_transaction(self, signature: str, mint: str, decimals: int) -> list[dict]:
        result = self._json_rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if not result:
            return []

        meta = result.get("meta") or {}
        msg = result.get("transaction", {}).get("message", {})
        account_keys = [
            a.get("pubkey") if isinstance(a, dict) else a
            for a in msg.get("accountKeys", [])
        ]

        # token account -> owner / mint 映射，用于把 source/destination 反查到 owner 钱包
        owner_by_account: dict[str, str] = {}
        mint_by_account: dict[str, str] = {}
        for bal_list in (meta.get("preTokenBalances", []), meta.get("postTokenBalances", [])):
            for b in bal_list:
                idx = b.get("accountIndex")
                if idx is None or idx >= len(account_keys):
                    continue
                addr = account_keys[idx]
                if b.get("owner"):
                    owner_by_account[addr] = b["owner"]
                if b.get("mint"):
                    mint_by_account[addr] = b["mint"]

        out: list[dict] = []
        instructions = list(msg.get("instructions", []))
        for inner in meta.get("innerInstructions", []):
            instructions.extend(inner.get("instructions", []))

        for ins in instructions:
            if not isinstance(ins, dict):
                continue
            if ins.get("program") != "spl-token":
                continue
            parsed = ins.get("parsed") or {}
            if parsed.get("type") not in ("transfer", "transferChecked"):
                continue
            info = parsed.get("info", {})
            source = info.get("source")
            destination = info.get("destination")
            if not source or not destination:
                continue
            # 仅保留该 mint 的转账（source/destination 属于该 mint）
            if mint_by_account.get(source) != mint and mint_by_account.get(destination) != mint:
                continue
            # transfer 的 amount 在 info.amount；transferChecked 嵌套在 info.tokenAmount.amount
            if parsed.get("type") == "transferChecked":
                amount_raw = (info.get("tokenAmount") or {}).get("amount")
            else:
                amount_raw = info.get("amount")
            if amount_raw is None:
                continue
            from_addr = owner_by_account.get(source, "")
            to_addr = owner_by_account.get(destination, "")
            out.append({
                "hash": signature,
                "blockNumber": str(result.get("slot", 0)),
                "timeStamp": str(result.get("blockTime", 0)),
                "from": from_addr,
                "to": to_addr,
                "value": str(amount_raw),
                "tokenDecimal": str(decimals),
                "contractAddress": mint,
                "tokenName": "",
                "tokenSymbol": "",
            })
        return out


def get_solana_client(api_key: str | None = None) -> SolanaClient:
    """获取 Solana 客户端（优先 Helius，缺 Key 回退公共 RPC）。"""
    return SolanaClient(api_key=api_key)
