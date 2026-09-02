"""GeckoTerminal 流动性查询客户端（MEME-03）。

GET /api/v2/networks/{network}/tokens/{addr}/pools → 聚合池子流动性。
"""
from __future__ import annotations

import os
import sys
import requests

for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)

SESSION = requests.Session()
SESSION.trust_env = False

TIMEOUT = 15
API_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{addr}/pools"

# chain → GeckoTerminal network slug
GECKO_NETWORK = {
    "ethereum": "ethereum",
    "bsc": "bsc",
    "solana": "solana",
    "base": "base",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "avalanche": "avax",
    "optimism": "optimism",
    "fantom": "fantom",
}


def get_liquidity(chain: str, addr: str) -> dict:
    """查询单个地址在 GeckoTerminal 上的流动性，返回标准化 dict。"""
    net = GECKO_NETWORK.get((chain or "").lower())
    if not net:
        return {"source": "geckoterminal", "source_status": "na"}

    url = API_URL.format(network=net, addr=addr)
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data") or []
    except Exception:
        return {"source": "geckoterminal", "source_status": "error"}

    if not data:
        return {"source": "geckoterminal", "source_status": "not_cached"}

    liqs: list[float] = []
    for d in data:
        attrs = d.get("attributes", {})
        usd = attrs.get("reserve_in_usd") or attrs.get("total_liquidity_usd")
        if usd:
            liqs.append(float(usd))

    if not liqs:
        return {"source": "geckoterminal", "source_status": "not_cached"}

    total = sum(liqs)
    top = max(liqs, default=0)

    return {
        "source": "geckoterminal",
        "source_status": "hit",
        "pool_count": len(liqs),
        "total_liquidity_usd": round(total, 2),
        "top_pool_share_pct": round(top / total * 100, 2) if total else None,
        "raw_json": data[:20],
    }
