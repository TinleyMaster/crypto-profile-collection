"""
多源交叉验证评分引擎。
融合 Binance Web3 + CoinMarketCap 数据，按多源共识度评分排序。
"""

from __future__ import annotations

import time
from typing import Any

from binance_market import get_hot_tokens as get_binance_tokens
from cmc_market import get_cmc_tokens, get_cmc_trending

# 可选依赖：从 scripts/src 引入赛道映射（app.py 已把 scripts/src 加 sys.path）。
# 独立运行本模块时回退为「无赛道信息」的默认评分。
try:
    from crypto_research.config import get_settings
    from crypto_research.db.conn import get_connection
    import psycopg.rows
    _HAS_DB = True
except ImportError:  # pragma: no cover - 独立运行场景
    _HAS_DB = False

# 权重分配
WEIGHTS = {
    "binance": 0.50,  # Binance 交易数据
    "cmc": 0.50,      # CMC 市场数据
}

# 缓存
_cache: dict[str, Any] = {}
_cache_ts: float = 0
CACHE_TTL = 120

# symbol → sector 映射缓存（赛道变化不频繁，TTL 拉长）
_sector_map: dict[str, str] = {}
_sector_map_ts: float = 0
SECTOR_MAP_TTL = 3600


def _normalize_float(v: Any, default: float = 0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_contract(contract: str) -> str:
    """归一化合约地址：EVM（0x 开头）统一小写，非 EVM（如 Solana base58）保持原样。"""
    c = (contract or "").strip()
    if not c:
        return ""
    return c.lower() if c.startswith("0x") else c


def _token_key(token: dict) -> str:
    """以合约地址作为 token 唯一标识；无合约地址时回退到 symbol。

    避免同名 symbol 的不同代币（如多个「牛来」meme）互相污染 name/contract。
    """
    contract = _norm_contract(token.get("contract", ""))
    if contract:
        return contract
    return "sym:" + (token.get("symbol") or "").strip().upper()


def _build_index(tokens: list[dict]) -> dict[str, dict]:
    """按合约地址（缺省回退 symbol）建立索引。"""
    return {_token_key(t): t for t in tokens if t.get("symbol")}


def _load_sector_map() -> dict[str, str]:
    """从 core.asset 加载 symbol → primary_sector 映射（带缓存）。

    处理同名 symbol 重复：排除 other 后取众数赛道；全部为 other 则回退 other。
    """
    global _sector_map, _sector_map_ts
    if not _HAS_DB:
        return {}

    now = time.time()
    if _sector_map and (now - _sector_map_ts) < SECTOR_MAP_TTL:
        return _sector_map

    try:
        settings = get_settings()
        with get_connection(settings.database_url) as conn:
            cur = conn.cursor(row_factory=psycopg.rows.dict_row)
            cur.execute("""
                SELECT DISTINCT ON (a.canonical_symbol)
                       a.canonical_symbol AS symbol,
                       a.primary_sector AS sector
                FROM core.asset a
                WHERE a.primary_sector IS NOT NULL
                  AND a.primary_sector != 'other'
                ORDER BY a.canonical_symbol,
                         CASE a.asset_type
                             WHEN 'coin' THEN 0
                             WHEN 'token' THEN 1
                             WHEN 'stablecoin' THEN 2
                             ELSE 3
                         END,
                         a.asset_id
            """)
            _sector_map = {r["symbol"].upper(): r["sector"] for r in cur.fetchall()}
            _sector_map_ts = now
    except Exception:
        # 赛道映射加载失败不影响主流程，回退默认评分
        _sector_map = {}
        _sector_map_ts = now

    return _sector_map


def _compute_consensus(binance_idx: dict, cmc_idx: dict) -> list[dict]:
    """计算多源交叉验证结果。"""
    all_keys = set(binance_idx.keys()) | set(cmc_idx.keys())
    results = []

    for key in all_keys:
        b = binance_idx.get(key)
        c = cmc_idx.get(key)

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

        # 选择最佳展示数据（symbol/name/chain/contract 均来自匹配到的同一 token）
        symbol = (b.get("symbol") if b else c.get("symbol", "")).upper() if (b or c) else ""
        price = b.get("price") if b else c.get("price") if c else 0
        change_24h = b_change if b else c_change
        volume_24h = b_vol if b_vol > 0 else c_vol

        # 项目名称：优先 CMC（有 name 字段），其次 Binance
        name = (c.get("name") if c else "") or (b.get("name") if b else "")
        # 链和合约地址：优先 Binance（有完整字段），否则回退 CMC（仅有合约）
        chain = b.get("chain", "") if b else c.get("chain", "")
        contract = b.get("contract", "") if b else c.get("contract", "")

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

    # 并行获取各数据源（Binance 评分套用分赛道权重）
    sector_map = _load_sector_map()
    binance_data = get_binance_tokens(100, sector_map)
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