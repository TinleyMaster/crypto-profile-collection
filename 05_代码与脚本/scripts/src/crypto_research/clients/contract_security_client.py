"""合约安全扫描：GoPlus(EVM) + RugCheck(Solana) + SolanaClient 兜底。

仿 derivatives_client.py 匿名 REST 范式。
EVM 走 GoPlus，Solana 走 RugCheck（404 降级 SolanaClient 取 mint/freeze authority）。
"""
from __future__ import annotations

import os

import requests

from .solana_client import SolanaClient

# GoPlus EVM chain_id 映射（仅覆盖项目主链）
EVM_CHAIN_ID: dict[str, int] = {
    "ethereum": 1, "bsc": 56, "base": 8453, "polygon": 137,
    "arbitrum": 42161, "avalanche": 43114, "optimism": 10,
    "linea": 59144, "blast": 81457, "zksync": 324,
}

TIMEOUT = 12


def _b(v) -> bool | None:
    """GoPlus 布尔字段解析：1/true→True，0/false→False，其余→None。"""
    s = str(v).lower()
    if s in ("1", "true"):
        return True
    if s in ("0", "false"):
        return False
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


class ContractSecurityClient:
    """合约安全扫描客户端：EVM→GoPlus，Solana→RugCheck+SolanaClient兜底。"""

    def scan(self, asset_id: int, chain: str, contract_addr: str) -> dict:
        """按 chain 分流扫描，返回统一结构 dict。"""
        if chain in EVM_CHAIN_ID:
            return self._goplus(asset_id, chain, contract_addr, EVM_CHAIN_ID[chain])
        if chain == "solana":
            return self._rugcheck(asset_id, contract_addr)
        # 非 EVM 非 Solana 链（tron/ton/aptos 等）暂不支持
        return {"asset_id": asset_id, "chain": chain, "source": "none", "source_status": "na"}

    def _goplus(self, asset_id: int, chain: str, addr: str, chain_id: int) -> dict:
        """GoPlus token security API（免费档）。"""
        url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
        try:
            r = requests.get(url, params={"contract_addresses": addr}, timeout=TIMEOUT)
            if r.status_code != 200:
                return {"asset_id": asset_id, "chain": chain, "source": "goplus",
                        "source_status": "error"}
            d = (r.json().get("result") or {}).get(addr.lower()) or \
                (r.json().get("result") or {}).get(addr)
            if not d:
                return {"asset_id": asset_id, "chain": chain, "source": "goplus",
                        "source_status": "not_cached"}
            return {
                "asset_id": asset_id, "chain": chain, "source": "goplus",
                "source_status": "hit",
                "is_honeypot": _b(d.get("is_honeypot")),
                "is_open_source": _b(d.get("is_open_source")),
                "is_mintable": _b(d.get("is_mintable")),
                "can_take_back_ownership": _b(d.get("can_take_back_ownership")),
                "hidden_owner": _b(d.get("hidden_owner")),
                "is_blacklisted": _b(d.get("is_blacklisted")),
                "freeze_authority": None,
                "mint_authority": d.get("owner_address"),
                "buy_tax": _num(d.get("buy_tax")),
                "sell_tax": _num(d.get("sell_tax")),
                "lp_locked_pct": None,
                "top_holders_pct": None,
                "holder_count": _int(d.get("holder_count")),
                "creator_percent": _num(d.get("creator_percent")),
                "risk_score": None,
                "raw_json": d,
            }
        except Exception:
            return {"asset_id": asset_id, "chain": chain, "source": "goplus",
                    "source_status": "error"}

    def _rugcheck(self, asset_id: int, mint: str) -> dict:
        """RugCheck Solana report summary API + SolanaClient 兜底。"""
        url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
        try:
            r = requests.get(url, headers={"Accept": "application/json", "User-Agent": "M"},
                             timeout=TIMEOUT)
        except Exception:
            return {"asset_id": asset_id, "chain": "solana", "source": "rugcheck",
                    "source_status": "error"}

        if r.status_code == 404:
            # 免费层未缓存 → SolanaClient 兜底取 mint/freeze authority
            rpc = SolanaClient(api_key=os.getenv("HELIUS_API_KEY"))
            auth = rpc.get_mint_authorities(mint)
            base = {"asset_id": asset_id, "chain": "solana", "source": "solana_rpc",
                    "source_status": "not_cached"}
            if auth:
                base.update({
                    "mint_authority": auth.get("mint_authority"),
                    "freeze_authority": auth.get("freeze_authority"),
                })
            return base

        if r.status_code != 200:
            return {"asset_id": asset_id, "chain": "solana", "source": "rugcheck",
                    "source_status": "error"}

        d = r.json()
        freeze_auth = None
        if isinstance(d.get("freezeAuthority"), dict):
            freeze_auth = d["freezeAuthority"].get("account")
        elif d.get("freezeAuthority"):
            freeze_auth = str(d["freezeAuthority"])

        mint_auth = None
        if isinstance(d.get("mintAuthority"), dict):
            mint_auth = d["mintAuthority"].get("account")
        elif d.get("mintAuthority"):
            mint_auth = str(d["mintAuthority"])

        return {
            "asset_id": asset_id, "chain": "solana", "source": "rugcheck",
            "source_status": "hit",
            "is_honeypot": None,
            "is_open_source": None,
            "is_mintable": None,
            "can_take_back_ownership": None,
            "hidden_owner": None,
            "is_blacklisted": None,
            "freeze_authority": freeze_auth,
            "mint_authority": mint_auth,
            "buy_tax": None,
            "sell_tax": None,
            "lp_locked_pct": _num(d.get("lpLockedPct")),
            "top_holders_pct": _num(d.get("topHoldersPct")),
            "holder_count": None,
            "creator_percent": None,
            "risk_score": _num(d.get("score_normalised")),
            "raw_json": d,
        }
