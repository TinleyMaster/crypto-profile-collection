"""
Binance Web3 市场热点数据获取与评分模块。
从币安统一代币排行接口获取实时市场数据，按投研价值评分排序。
"""

from __future__ import annotations

import requests
import time
from typing import Any

BASE_URL = "https://web3.binance.com"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
}
TIMEOUT = 10

# API 端点
RANK_URL = f"{BASE_URL}/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list"

# 评分权重
SCORE_WEIGHTS = {
    "volume": 0.30,       # 24h 交易量
    "change_24h": 0.25,   # 24h 涨跌幅
    "txns": 0.20,         # 交易笔数
    "buy_ratio": 0.15,    # 买入占比
    "momentum": 0.10,     # 短期动量（1h/5m 变化）
}

# 过滤阈值
MIN_VOLUME_USD = 5000       # 最低 24h 交易量
MIN_TXNS = 30               # 最低交易笔数
MAX_CHANGE_24H = 1000       # 最多 1000% 涨幅（过滤极端异常）
MIN_CHANGE_24H = -90        # 最少 -90% 跌幅

# Binance chainId → 可读链名
CHAIN_NAME_MAP = {
    "1": "Ethereum",
    "10": "Optimism",
    "56": "BSC",
    "137": "Polygon",
    "250": "Fantom",
    "324": "zkSync Era",
    "8453": "Base",
    "42161": "Arbitrum",
    "43114": "Avalanche",
    "CT_501": "Solana",
}


def _chain_name(chain_id: Any) -> str:
    """将 Binance chainId 映射为可读链名，未知时回退原值。"""
    if chain_id is None:
        return ""
    return CHAIN_NAME_MAP.get(str(chain_id), str(chain_id))

# 缓存
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 120  # 2 分钟缓存


def _fetch_pages(pages: int = 1, page_size: int = 50) -> list[dict]:
    """获取统一代币排行数据。API 返回所有结果，无需分页。"""
    try:
        r = requests.post(
            RANK_URL,
            headers=HEADERS,
            json={"page": 1, "pageSize": page_size},
            timeout=TIMEOUT,
        )
        data = r.json()
        if data.get("success") and data.get("data", {}).get("tokens"):
            return data["data"]["tokens"]
    except Exception:
        pass
    return []


def _normalize(value: float, min_val: float, max_val: float) -> float:
    """Min-max 归一化到 0-1。"""
    if max_val <= min_val:
        return 0.5
    return max(0, min(1, (value - min_val) / (max_val - min_val)))


def _safe_float(v: Any, default: float = 0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def score_tokens(raw_tokens: list[dict]) -> list[dict]:
    """对原始代币数据评分排序。"""
    scored = []

    # 数据清洗
    for t in raw_tokens:
        symbol = (t.get("symbol") or "").strip()
        if not symbol:
            continue

        change_24h = _safe_float(t.get("percentChange24h"))
        volume_24h = _safe_float(t.get("volume24h"))
        txns_24h = _safe_int(t.get("count24h"))
        buys_24h = _safe_int(t.get("count24hBuy"))
        sells_24h = _safe_int(t.get("count24hSell"))
        change_1h = _safe_float(t.get("percentChange1h"))
        change_5m = _safe_float(t.get("percentChange5m"))
        price = _safe_float(t.get("price"))

        # 过滤
        if volume_24h < MIN_VOLUME_USD:
            continue
        if txns_24h < MIN_TXNS:
            continue
        if change_24h > MAX_CHANGE_24H or change_24h < MIN_CHANGE_24H:
            continue

        # 买入占比
        total_buysell = buys_24h + sells_24h
        buy_ratio = buys_24h / total_buysell if total_buysell > 0 else 0.5

        scored.append({
            "symbol": symbol,
            "name": (t.get("name") or "").strip(),
            "chain": _chain_name(t.get("chainId", "")),
            "contract": t.get("contractAddress", ""),
            "price": price,
            "change_24h": change_24h,
            "change_1h": change_1h,
            "change_5m": change_5m,
            "volume_24h": volume_24h,
            "txns_24h": txns_24h,
            "buys_24h": buys_24h,
            "sells_24h": sells_24h,
            "buy_ratio": buy_ratio,
            "icon": t.get("icon", ""),
        })

    if not scored:
        return []

    # 计算各维度极值（用于归一化）
    volumes = [s["volume_24h"] for s in scored]
    txns_list = [s["txns_24h"] for s in scored]
    changes = [s["change_24h"] for s in scored]
    buy_ratios = [s["buy_ratio"] for s in scored]
    abs_changes = [abs(s["change_1h"]) + abs(s["change_5m"]) * 2 for s in scored]

    v_min, v_max = min(volumes), max(volumes)
    t_min, t_max = min(txns_list), max(txns_list)
    c_min, c_max = min(changes), max(changes)
    b_min, b_max = min(buy_ratios), max(buy_ratios)
    m_min, m_max = min(abs_changes), max(abs_changes)

    for s in scored:
        vol_score = _normalize(s["volume_24h"], v_min, v_max)
        txn_score = _normalize(s["txns_24h"], t_min, t_max)
        change_score = _normalize(s["change_24h"], c_min, c_max)
        buy_score = _normalize(s["buy_ratio"], b_min, b_max)
        momentum = abs(s["change_1h"]) + abs(s["change_5m"]) * 2
        momentum_score = _normalize(momentum, m_min, m_max)

        s["score"] = round(
            vol_score * SCORE_WEIGHTS["volume"] * 100
            + change_score * SCORE_WEIGHTS["change_24h"] * 100
            + txn_score * SCORE_WEIGHTS["txns"] * 100
            + buy_score * SCORE_WEIGHTS["buy_ratio"] * 100
            + momentum_score * SCORE_WEIGHTS["momentum"] * 100,
            1,
        )

        # 各维度评分明细
        s["score_detail"] = {
            "volume": round(vol_score * 100, 1),
            "change": round(change_score * 100, 1),
            "txns": round(txn_score * 100, 1),
            "buy_ratio": round(buy_score * 100, 1),
            "momentum": round(momentum_score * 100, 1),
        }

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def get_hot_tokens(limit: int = 30) -> dict:
    """获取今日最值得投研的代币列表（带缓存）。"""
    global _cache, _cache_ts

    now = time.time()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        return {"cached": True, "tokens": _cache["tokens"][:limit], "total": _cache["total"]}

    raw = _fetch_pages(page_size=200)
    scored = score_tokens(raw)

    result = {
        "cached": False,
        "tokens": scored[:limit],
        "total": len(scored),
        "fetched_at": int(now),
    }

    _cache = {"tokens": scored, "total": len(scored)}
    _cache_ts = now

    return result


def get_top_gainers(limit: int = 10) -> list[dict]:
    """24h 涨幅最高（已过滤极端值）。"""
    raw = _fetch_pages(page_size=200)
    scored = score_tokens(raw)
    return sorted(scored, key=lambda x: x["change_24h"], reverse=True)[:limit]


def get_top_volume(limit: int = 10) -> list[dict]:
    """24h 交易量最高。"""
    raw = _fetch_pages(page_size=200)
    scored = score_tokens(raw)
    return sorted(scored, key=lambda x: x["volume_24h"], reverse=True)[:limit]