"""
CoinMarketCap 市场数据获取模块。
使用 CMC 公开 keyless API，获取热门代币排行和交易数据。
"""

from __future__ import annotations

import requests
import time
from typing import Any

CMC_BASE = "https://pro-api.coinmarketcap.com"
TIMEOUT = 10

# 缓存
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 120  # 2 分钟


def _fetch_listings(sort: str = "volume_24h", limit: int = 200) -> list[dict]:
    """获取 CMC 代币排行数据（公开 keyless API）。"""
    try:
        r = requests.get(
            f"{CMC_BASE}/public-api/v3/cryptocurrency/listings/latest",
            params={"limit": limit, "sort": sort, "sort_dir": "desc"},
            timeout=TIMEOUT,
        )
        data = r.json()
        return data.get("data", [])
    except Exception:
        return []


def _normalize_cmc_listing(item: dict) -> dict | None:
    """将 CMC 原始数据标准化为统一格式。"""
    symbol = (item.get("symbol") or "").strip()
    if not symbol:
        return None

    quotes = item.get("quote", [])
    if not quotes:
        return None
    usd = quotes[0]

    volume_24h = float(usd.get("volume_24h", 0) or 0)
    change_24h = float(usd.get("percent_change_24h", 0) or 0)
    change_1h = float(usd.get("percent_change_1h", 0) or 0)
    price = float(usd.get("price", 0) or 0)
    market_cap = float(usd.get("market_cap", 0) or 0)

    return {
        "symbol": symbol,
        "name": item.get("name", ""),
        "slug": item.get("slug", ""),
        "cmc_rank": item.get("cmc_rank", 0),
        "price": price,
        "change_24h": change_24h,
        "change_1h": change_1h,
        "volume_24h": volume_24h,
        "market_cap": market_cap,
        "num_market_pairs": item.get("num_market_pairs", 0),
        "tags": item.get("tags", []),
    }


def get_cmc_tokens(limit: int = 30) -> dict:
    """获取 CMC 热门代币（按交易量排序），带缓存。"""
    global _cache, _cache_ts

    now = time.time()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        cached = _cache["tokens"][:limit]
        return {"cached": True, "tokens": cached, "total": _cache["total"]}

    raw = _fetch_listings(sort="volume_24h", limit=200)
    tokens = []
    for item in raw:
        normalized = _normalize_cmc_listing(item)
        if normalized:
            tokens.append(normalized)

    _cache = {"tokens": tokens, "total": len(tokens)}
    _cache_ts = now

    return {
        "cached": False,
        "tokens": tokens[:limit],
        "total": len(tokens),
        "fetched_at": int(now),
    }


def get_cmc_trending(limit: int = 30) -> dict:
    """获取 CMC 涨幅榜（按 24h 涨幅排序）。"""
    raw = _fetch_listings(sort="percent_change_24h", limit=200)
    # 过滤掉稳定币和极端值
    tokens = []
    for item in raw:
        normalized = _normalize_cmc_listing(item)
        if not normalized:
            continue
        # 过滤稳定币
        if "stablecoin" in normalized.get("tags", []):
            continue
        # 过滤极端涨跌
        if abs(normalized["change_24h"]) > 500:
            continue
        tokens.append(normalized)

    return {
        "tokens": tokens[:limit],
        "total": len(tokens),
    }