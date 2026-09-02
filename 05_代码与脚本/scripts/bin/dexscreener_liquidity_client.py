"""DexScreener 流动性查询客户端（MEME-03）。

GET /latest/dex/tokens/{addr} → 聚合 pairs 流动性。
复用 populate_contracts_from_dexscreener.py 的 SESSION 守卫范式。
"""
from __future__ import annotations

import os
import sys
import requests

# 清除代理变量：本地 socks5 代理不可用
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)

SESSION = requests.Session()
SESSION.trust_env = False

TIMEOUT = 15
API_URL = "https://api.dexscreener.com/latest/dex/tokens/{addr}"


def get_liquidity(addr: str) -> dict:
    """查询单个合约地址的 DEX 流动性，返回标准化 dict。"""
    url = API_URL.format(addr=addr)
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception:
        return {"source": "dexscreener", "source_status": "error"}

    if not pairs:
        return {"source": "dexscreener", "source_status": "not_cached"}

    # 过滤有流动性的池子
    pairs = [p for p in pairs if (p.get("liquidity") or {}).get("usd")]
    if not pairs:
        return {"source": "dexscreener", "source_status": "not_cached"}

    total = sum(p["liquidity"]["usd"] for p in pairs)
    top = max((p["liquidity"]["usd"] for p in pairs), default=0)

    return {
        "source": "dexscreener",
        "source_status": "hit",
        "pool_count": len(pairs),
        "total_liquidity_usd": round(total, 2),
        "top_pool_share_pct": round(top / total * 100, 2) if total else None,
        "raw_json": pairs[:20],
    }
