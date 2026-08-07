"""
多源交叉验证评分引擎。
融合 Binance Web3 + CoinMarketCap 数据，按多源共识度评分排序。
"""

from __future__ import annotations

import time
from typing import Any

from binance_market import get_hot_tokens as get_binance_tokens
from cmc_market import get_cmc_tokens, get_cmc_trending

# 权重分配
WEIGHTS = {
    "binance": 0.50,  # Binance 交易数据
    "cmc": 0.50,      # CMC 市场数据
}

# 缓存
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 120


def _normalize_float(v: Any, default: float = 0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _build_index(tokens: list[dict], key: str = "symbol") -> dict[str, dict]:
    """按 symbol 建立索引。"""
    return {t.get(key, "").upper(): t for t in tokens if t.get(key)}


def _compute_consensus(binance_idx: dict, cmc_idx: dict) -> list[dict]:
    """计算多源交叉验证结果。"""
    all_symbols = set(binance_idx.keys()) | set(cmc_idx.keys())
    results = []

    for sym in all_symbols:
        b = binance_idx.get(sym)
        c = cmc_idx.get(sym)

        sources = []
        # Binance 信号
        b_score = _normalize_float(b.get("score", 0)) if b else 0
        b_change = _normalize_float(b.get("change_24h", 0)) if b else 0
        b_vol = _normalize_float(b.get("volume_24h", 0)) if b else 0
        if b:
            sources.append("binance")

        # CMC 信号
        c_rank = c.get("cmc_rank", 99999) if c else 99999
        c_change = _normalize_float(c.get("change_24h", 0)) if c else 0
        c_vol = _normalize_float(c.get("volume_24h", 0)) if c else 0
        if c:
            sources.append("cmc")

        # 共识等级
        source_count = len(sources)
        if source_count >= 2:
            consensus = "3/3" if source_count == 3 else "2/3"
        else:
            consensus = "1/3"

        # CMC 标准化评分：rank 越小越好，volume 越大越好
        c_rank_score = max(0, 100 - c_rank * 0.1) if c_rank < 1000 else 0
        c_score = c_rank_score * 0.5 + min(100, c_change + 50) * 0.5

        # 综合评分
        composite = (
            b_score * WEIGHTS["binance"]
            + c_score * WEIGHTS["cmc"]
        )

        # 选择最佳展示数据
        symbol = (b.get("symbol") if b else c.get("symbol", sym)).upper() if (b or c) else sym
        price = b.get("price") if b else c.get("price") if c else 0
        change_24h = b_change if b else c_change
        volume_24h = b_vol if b_vol > 0 else c_vol

        # 项目名称：优先 CMC（有 name 字段），其次 Binance
        name = (c.get("name") if c else "") or (b.get("name") if b else "")
        # 链和合约地址：仅 Binance 有
        chain = b.get("chain", "") if b else ""
        contract = b.get("contract", "") if b else ""

        results.append({
            "symbol": symbol,
            "name": name,
            "chain": chain,
            "contract": contract,
            "price": _normalize_float(price),
            "change_24h": change_24h,
            "volume_24h": volume_24h,
            "composite_score": round(composite, 1),
            "binance_score": round(b_score, 1),
            "cmc_rank": c_rank if c_rank < 99999 else None,
            "consensus": consensus,
            "sources": sources,
            "source_count": source_count,
            # 详细数据
            "binance": b,
            "cmc": c,
        })

    # 排序：共识度 > 综合评分
    results.sort(key=lambda x: (x["source_count"], x["composite_score"]), reverse=True)
    return results


def get_cross_validated(limit: int = 30) -> dict:
    """获取多源交叉验证的投研推荐（带缓存）。"""
    global _cache, _cache_ts

    now = time.time()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        return {"cached": True, "results": _cache["results"][:limit], "total": _cache["total"]}

    # 并行获取各数据源
    binance_data = get_binance_tokens(100)
    cmc_data = get_cmc_tokens(100)

    binance_idx = _build_index(binance_data.get("tokens", []))
    cmc_idx = _build_index(cmc_data.get("tokens", []))

    results = _compute_consensus(binance_idx, cmc_idx)

    _cache = {"results": results, "total": len(results)}
    _cache_ts = now

    return {
        "cached": False,
        "results": results[:limit],
        "total": len(results),
        "fetched_at": int(now),
        "source_stats": {
            "binance": len(binance_idx),
            "cmc": len(cmc_idx),
            "both": sum(1 for r in results if r["source_count"] >= 2),
        },
    }


def get_consensus_gainers(limit: int = 30) -> dict:
    """获取多源共识的涨幅榜。"""
    result = get_cross_validated(100)
    # 按 24h 涨幅排序，优先高共识
    gainers = sorted(
        result["results"],
        key=lambda x: (x["source_count"], x["change_24h"]),
        reverse=True,
    )
    return {
        "results": gainers[:limit],
        "total": len(gainers),
    }


def get_consensus_volume(limit: int = 30) -> dict:
    """获取多源共识的交易量榜。"""
    result = get_cross_validated(100)
    volume_ranked = sorted(
        result["results"],
        key=lambda x: (x["source_count"], x["volume_24h"]),
        reverse=True,
    )
    return {
        "results": volume_ranked[:limit],
        "total": len(volume_ranked),
    }