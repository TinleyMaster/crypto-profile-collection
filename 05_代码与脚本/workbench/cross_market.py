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


def _compute_consensus(binance_idx: dict, cmc_idx: dict, sector_map: dict[str, str] | None = None) -> list[dict]:
    """计算多源交叉验证结果。

    Args:
        binance_idx: Binance 代币索引
        cmc_idx: CMC 代币索引
        sector_map: symbol(大写) → primary_sector 映射，用于注入赛道字段
    """
    sector_map = sector_map or {}
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
        c_mcap = _normalize_float(c.get("market_cap", 0)) if c else 0
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
            "sector": sector_map.get(symbol, "other"),
            "price": _normalize_float(price),
            "change_24h": change_24h,
            "volume_24h": volume_24h,
            "market_cap": c_mcap if c_mcap > 0 else None,
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

    results = _compute_consensus(binance_idx, cmc_idx, sector_map)

    # 实时数据源都为空时，fallback 到今日存档数据（外部 API 挂了也能展示）
    if not results and _HAS_DB:
        try:
            from db_stats import get_db
            import psycopg.rows
            with get_db() as conn:
                with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                    cur.execute("""
                        SELECT symbol, name, chain, contract, sector,
                               source_count, composite_score, change_24h,
                               volume_24h, price_usd AS price, market_cap_usd AS market_cap,
                               rank
                        FROM biz.daily_recommendation
                        WHERE rec_date = CURRENT_DATE
                        ORDER BY rank ASC
                        LIMIT %s
                    """, (max(limit, 100),))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        _cache = {"results": rows, "total": len(rows)}
                        _cache_ts = now
                        return {
                            "cached": False,
                            "results": rows[:limit],
                            "total": len(rows),
                            "fetched_at": int(now),
                            "from_archive": True,
                            "source_stats": {"binance": 0, "cmc": 0, "both": 0},
                        }
        except Exception:
            pass  # fallback 失败不影响主流程

    _cache = {"results": results, "total": len(results)}
    _cache_ts = now

    # 异步存档到 DB（不阻塞返回）
    try:
        _archive_daily_recommendations(results)
    except Exception:
        pass  # 存档失败不影响主流程

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


def _archive_daily_recommendations(results: list[dict]) -> None:
    """将每日推荐存档到 biz.daily_recommendation，按天去重（同一天只存第一次）。

    用于后续回测推荐质量。
    """
    import datetime
    from db_stats import get_db
    import psycopg

    today = datetime.date.today()

    with get_db() as conn:
        # 确保表存在
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz.daily_recommendation (
                    rec_date DATE NOT NULL,
                    rank INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    chain TEXT,
                    contract TEXT,
                    sector TEXT,
                    source_count INTEGER,
                    composite_score NUMERIC(6,2),
                    change_24h NUMERIC(8,2),
                    volume_24h NUMERIC(20,2),
                    price_usd NUMERIC(18,8),
                    market_cap_usd NUMERIC(20,2),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (rec_date, symbol, chain)
                )
            """)

        # 检查今天是否已存档（避免重复写入）
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM biz.daily_recommendation WHERE rec_date = %s",
                (today,),
            )
            if cur.fetchone()[0] > 0:
                return  # 今天已存档，跳过

        # 批量插入前 30 名
        with conn.cursor() as cur:
            for i, t in enumerate(results[:30]):
                try:
                    cur.execute("""
                        INSERT INTO biz.daily_recommendation
                            (rec_date, rank, symbol, name, chain, contract, sector,
                             source_count, composite_score, change_24h, volume_24h,
                             price_usd, market_cap_usd)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (rec_date, symbol, chain) DO NOTHING
                    """, (
                        today,
                        i + 1,
                        t.get("symbol", ""),
                        t.get("name", ""),
                        t.get("chain", ""),
                        t.get("contract", ""),
                        t.get("sector", ""),
                        t.get("source_count", 0),
                        t.get("composite_score", 0),
                        t.get("change_24h", 0),
                        t.get("volume_24h", 0),
                        t.get("price", 0),
                        t.get("market_cap", 0),
                    ))
                except (psycopg.errors.UniqueViolation, psycopg.errors.IntegrityError):
                    continue
        conn.commit()


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


def get_sector_heatmap(limit: int = 20) -> dict:
    """赛道轮动热力图：按赛道聚合多源交叉验证结果，输出各赛道热度指标。

    指标：
      - token_count: 上榜代币数
      - avg_change_24h: 平均 24h 涨幅
      - avg_score: 平均综合评分
      - top_token: 该赛道评分最高的代币
      - heat_score: 综合热度分（上榜数 × 平均涨幅 × 平均评分，归一化后 0-100）

    防污染机制：
      - 最低市值过滤：市值 < $1M 的代币不参与统计（过滤土狗）
      - 涨幅截断：单币 24h 涨幅 > 200% 按 200% 计（防止极端值拉动均值）
      - 样本量标注：token_count < 3 的赛道标注 low_sample=True
    """
    result = get_cross_validated(300)
    tokens = result["results"]

    # 过滤：市值 >= $1M（多源 fallback 估算）
    MIN_MCAP = 1_000_000
    MAX_CHANGE = 200.0  # 单币涨幅上限，防极端值
    filtered = []
    for t in tokens:
        mcap = t.get("market_cap") or 0
        if not mcap:
            # 用成交量粗略估算：volume_24h > $100k 的也保留
            vol = t.get("volume_24h") or 0
            if vol < 100_000:
                continue
        else:
            if mcap < MIN_MCAP:
                continue
        # 涨幅截断
        t = dict(t)
        if t["change_24h"] > MAX_CHANGE:
            t["change_24h"] = MAX_CHANGE
            t["change_capped"] = True
        filtered.append(t)

    # 按赛道分组
    sectors: dict[str, list[dict]] = {}
    for t in filtered:
        sec = t.get("sector") or "other"
        sectors.setdefault(sec, []).append(t)

    # 计算各赛道指标
    sector_stats = []
    for sec, stokens in sectors.items():
        count = len(stokens)
        avg_change = sum(t["change_24h"] for t in stokens) / count
        avg_score = sum(t["composite_score"] for t in stokens) / count
        top = max(stokens, key=lambda x: x["composite_score"])

        # 热度分：上榜数权重 40% + 平均涨幅权重 30% + 平均评分权重 30%
        # 归一化基准：count 以 10 为满值，avg_change 以 20% 为满值，avg_score 以 80 为满值
        count_norm = min(100, count / 10 * 100)
        change_norm = min(100, max(0, avg_change) / 20 * 100)
        score_norm = min(100, avg_score / 80 * 100)
        heat_score = round(count_norm * 0.4 + change_norm * 0.3 + score_norm * 0.3, 1)

        sector_stats.append({
            "sector": sec,
            "token_count": count,
            "avg_change_24h": round(avg_change, 2),
            "avg_score": round(avg_score, 1),
            "heat_score": heat_score,
            "low_sample": count < 3,
            "top_token": {
                "symbol": top["symbol"],
                "name": top.get("name", ""),
                "change_24h": top["change_24h"],
                "composite_score": top["composite_score"],
            },
        })

    # 按热度分降序
    sector_stats.sort(key=lambda x: x["heat_score"], reverse=True)

    return {
        "sectors": sector_stats[:limit],
        "total_sectors": len(sector_stats),
        "total_tokens": len(filtered),
        "filtered_out": len(tokens) - len(filtered),
        "fetched_at": result.get("fetched_at"),
    }