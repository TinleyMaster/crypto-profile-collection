from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _iso_ts(value: Any) -> str | None:
    """把 CMC 返回的 ISO 时间戳字符串归一化为 ISO 格式（若已是 str 直接返回）。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════════════════════
# 全球市场指标 /v1/global-metrics/quotes/latest
# ════════════════════════════════════════════════════════════
def parse_global_metrics_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    quote = (data.get("quote") or {}).get("USD") or {}
    return {
        "total_market_cap": _safe_float(quote.get("total_market_cap")),
        "total_volume_24h": _safe_float(quote.get("total_volume_24h")),
        "btc_dominance": _safe_float(data.get("btc_dominance")),
        "eth_dominance": _safe_float(data.get("eth_dominance")),
        "stablecoin_market_cap": _safe_float(data.get("stablecoin_market_cap")),
        "total_cryptocurrencies": _safe_int(data.get("total_cryptocurrencies")),
        "active_cryptocurrencies": _safe_int(data.get("active_cryptocurrencies")),
    }


# ════════════════════════════════════════════════════════════
# 恐贪指数 /v3/fear-and-greed
# ════════════════════════════════════════════════════════════
def parse_fear_greed_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    return {
        "value": _safe_float(data.get("value")),
        "value_classification": data.get("value_classification"),
    }


def parse_fear_greed_history_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """恐贪指数历史序列。返回 [{timestamp, value, value_classification}, ...]"""
    data = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for item in data:
        rows.append(
            {
                "timestamp": _iso_ts(item.get("timestamp")),
                "value": _safe_float(item.get("value")),
                "value_classification": item.get("value_classification"),
            }
        )
    return rows


# ════════════════════════════════════════════════════════════
# 山寨季指数 /v1/altcoin-season-index
# ════════════════════════════════════════════════════════════
def parse_altcoin_season_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    return {
        "value": _safe_float(data.get("value")),
    }


# ════════════════════════════════════════════════════════════
# 趋势榜 /v1/cryptocurrency/trending/* 和 /v1/cryptocurrency/listings/new
# ════════════════════════════════════════════════════════════
def parse_trending_payload(
    payload: dict[str, Any],
    trend_type: str,
    time_period: str = "24h",
) -> list[dict[str, Any]]:
    """解析 CMC 趋势榜响应（gainers / losers / trending / most_visited / new）。

    Returns list of dicts with keys matching biz.asset_trending columns.
    """
    data = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for idx, coin in enumerate(data, start=1):
        cmc_id = _safe_int(coin.get("id"))
        if cmc_id is None:
            continue

        # 榜单币的 quote 结构可能为 dict（{USD: {...}}）或数组（v3）
        quote = coin.get("quote") or {}
        if isinstance(quote, list):
            quote_usd = quote[0] if quote else {}
        else:
            quote_usd = quote.get("USD") or {}

        rows.append(
            {
                "trend_type": trend_type,
                "time_period": time_period,
                "cmc_id": cmc_id,
                "symbol": coin.get("symbol"),
                "name": coin.get("name"),
                "slug": coin.get("slug"),
                "rank_num": idx,
                "price_usd": _safe_float(quote_usd.get("price")),
                "market_cap": _safe_float(quote_usd.get("market_cap")),
                "volume_24h": _safe_float(quote_usd.get("volume_24h")),
                "percent_change_24h": _safe_float(quote_usd.get("percent_change_24h")),
            }
        )
    return rows


# ════════════════════════════════════════════════════════════
# 空投 /v1/cryptocurrency/airdrops 和 /v1/cryptocurrency/airdrop
# ════════════════════════════════════════════════════════════
def parse_airdrop_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解析空投列表响应。"""
    data = payload.get("data") or []
    if isinstance(data, dict):
        # 单个 airdrop 详情接口：data 为 {id: {...}} 映射
        items = [v for v in data.values()]
    else:
        items = data

    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        coin = item.get("coin") or {}
        rows.append(
            {
                "airdrop_id": str(item.get("id")) if item.get("id") else None,
                "project_name": item.get("project_name"),
                "description": item.get("description"),
                "status": item.get("status"),
                "coin_id": _safe_int(coin.get("id")),
                "coin_symbol": coin.get("symbol"),
                "coin_name": coin.get("name"),
                "coin_slug": coin.get("slug"),
                "start_date": _iso_ts(item.get("start_date")),
                "end_date": _iso_ts(item.get("end_date")),
                "total_prize": _safe_float(item.get("total_prize")),
                "winner_count": _safe_int(item.get("winner_count")),
                "link": item.get("link"),
            }
        )
    return rows


# ════════════════════════════════════════════════════════════
# 历史 OHLCV /v2/cryptocurrency/ohlcv/historical
# ════════════════════════════════════════════════════════════
def parse_ohlcv_historical_payload(
    payload: dict[str, Any],
    time_period: str = "daily",
    raw_response_id: int | None = None,
) -> list[dict[str, Any]]:
    """解析历史 OHLCV 响应。data 结构：
    {
        "id": 1, "name": "...", "symbol": "...",
        "quotes": [
            {"time_open": "...", "time_close": "...", "time_high": "...", "time_low": "...",
             "quote": {"USD": {"open":.., "high":.., "low":.., "close":.., "volume":.., "market_cap":..}}}
        ]
    }
    """
    data = payload.get("data") or {}
    rows: list[dict[str, Any]] = []

    if isinstance(data, dict) and "quotes" in data:
        items: list[dict[str, Any]] = [data]
    elif isinstance(data, dict):
        # 多币种时 data 为 {cmc_id: {..., quotes: [...]}}
        items = [v for v in data.values() if isinstance(v, dict)]
    else:
        items = []

    for coin in items:
        cmc_id = _safe_int(coin.get("id"))
        if cmc_id is None:
            continue
        for q in coin.get("quotes") or []:
            quote = q.get("quote") or {}
            if isinstance(quote, list):
                quote_usd = quote[0] if quote else {}
            else:
                quote_usd = quote.get("USD") or {}
            rows.append(
                {
                    "cmc_id": cmc_id,
                    "time_open": _iso_ts(q.get("time_open")),
                    "time_period": time_period,
                    "open": _safe_float(quote_usd.get("open")),
                    "high": _safe_float(quote_usd.get("high")),
                    "low": _safe_float(quote_usd.get("low")),
                    "close": _safe_float(quote_usd.get("close")),
                    "volume": _safe_float(quote_usd.get("volume")),
                    "market_cap": _safe_float(quote_usd.get("market_cap")),
                    "raw_response_id": raw_response_id,
                }
            )
    return rows


# ════════════════════════════════════════════════════════════
# 价格表现统计 /v2/cryptocurrency/price-performance-stats/latest
# ════════════════════════════════════════════════════════════
def parse_perf_stats_payload(
    payload: dict[str, Any],
    raw_response_id: int | None = None,
) -> list[dict[str, Any]]:
    """解析价格表现统计响应。CMC 响应有两种嵌套结构（随版本/参数不同）：
    结构 A（按周期）：periods 的键是 time_period（如 all_time/yesterday/24h），值内含 quote
    结构 B（按计价币）：periods 的键是计价币符号（如 USD），值内含 quote（单周期请求时）

    data 结构：
    {
        "1": {
            "id": 1, "name": "...", "symbol": "...",
            "periods": {
                "all_time": {
                    "open_timestamp": "...", "high_timestamp": "...", "low_timestamp": "...",
                    "close_timestamp": "...",
                    "quote": {"USD": {"open":.., "high":.., "low":.., "close":..,
                                      "percent_change":.., "price_change":..}}
                }
            }
        }
    }
    """
    data = payload.get("data") or {}
    rows: list[dict[str, Any]] = []
    snapshot_time = datetime.now(timezone.utc)

    for cmc_id_str, coin in data.items():
        if not isinstance(coin, dict):
            continue
        cmc_id = _safe_int(cmc_id_str)
        if cmc_id is None:
            continue

        periods = coin.get("periods") or {}
        for period_label, period_data in periods.items():
            if not isinstance(period_data, dict):
                continue

            # 判断嵌套层级：若 period_data 内有 quote 键 → 这是结构 B（键=计价币），
            # time_period 用默认 all_time；否则键就是 time_period（结构 A）。
            if "quote" in period_data:
                actual_period = "all_time"
                stats_block = period_data
            else:
                actual_period = period_label
                # 结构 A：再往下找 quote（可能是 {USD:{...}} 或数组）
                stats_block = period_data

            quote_map = stats_block.get("quote") or {}
            if isinstance(quote_map, list):
                q = quote_map[0] if quote_map else {}
            else:
                q = (quote_map.get("USD") or {}) if isinstance(quote_map, dict) else {}

            rows.append(
                {
                    "cmc_id": cmc_id,
                    "snapshot_time": snapshot_time,
                    "time_period": actual_period,
                    "open": _safe_float(q.get("open")),
                    "high": _safe_float(q.get("high")),
                    "low": _safe_float(q.get("low")),
                    "close": _safe_float(q.get("close")),
                    "percent_change": _safe_float(q.get("percent_change")),
                    "price_change": _safe_float(q.get("price_change")),
                    "open_timestamp": _iso_ts(stats_block.get("open_timestamp")),
                    "high_timestamp": _iso_ts(stats_block.get("high_timestamp")),
                    "low_timestamp": _iso_ts(stats_block.get("low_timestamp")),
                    "close_timestamp": _iso_ts(stats_block.get("close_timestamp")),
                    "raw_response_id": raw_response_id,
                }
            )
    return rows


# ════════════════════════════════════════════════════════════
# 交易对快照 /v2/cryptocurrency/market-pairs/latest
# ════════════════════════════════════════════════════════════
def parse_market_pairs_payload(
    payload: dict[str, Any],
    raw_response_id: int | None = None,
) -> list[dict[str, Any]]:
    """解析交易对快照响应。data 结构：
    {
        "id": 1, "name": "...", "symbol": "...", "num_market_pairs": 7526,
        "market_pairs": [
            {"exchange": {"id":..,"name":"..","slug":".."}, "market_id":.., "market_pair":"BTC/USD",
             "category":"derivatives", "fee_type":"no-fees",
             "market_pair_base": {"currency_id":.., "currency_symbol":"..","currency_type":".."},
             "market_pair_quote": {...},
             "quote": {"exchange_reported": {...}, "USD": {...}}}
        ]
    }
    """
    data = payload.get("data") or {}
    rows: list[dict[str, Any]] = []
    snapshot_time = datetime.now(timezone.utc)

    items: list[dict[str, Any]] = []
    if isinstance(data, dict) and "market_pairs" in data:
        items = [data]
    elif isinstance(data, dict):
        items = [v for v in data.values() if isinstance(v, dict)]

    for coin in items:
        cmc_id = _safe_int(coin.get("id"))
        if cmc_id is None:
            continue
        for pair in coin.get("market_pairs") or []:
            quote = pair.get("quote") or {}
            if isinstance(quote, list):
                quote_usd = quote[0] if quote else {}
            else:
                quote_usd = quote.get("USD") or {}

            base = pair.get("market_pair_base") or {}
            quote_side = pair.get("market_pair_quote") or {}
            exchange = pair.get("exchange") or {}

            rows.append(
                {
                    "cmc_id": cmc_id,
                    "snapshot_time": snapshot_time,
                    "exchange_name": exchange.get("name"),
                    "market_pair": pair.get("market_pair"),
                    "market_type": quote_side.get("currency_type"),
                    "category": pair.get("category"),
                    "pair_base_symbol": base.get("currency_symbol"),
                    "pair_quote_symbol": quote_side.get("currency_symbol"),
                    "price": _safe_float(quote_usd.get("price")),
                    "volume_24h": _safe_float(quote_usd.get("volume_24h")),
                    "liquidity_usd": _safe_float(quote_usd.get("liquidity_usd")),
                    "market_url": pair.get("market_url"),
                    "outlier_score": _safe_float(pair.get("outlier_score")),
                    "effective_liquidity": _safe_float(pair.get("effective_liquidity")),
                    "raw_response_id": raw_response_id,
                }
            )
    return rows


# ════════════════════════════════════════════════════════════
# DEX Token 详情 /v1/dex/token
# ════════════════════════════════════════════════════════════
def parse_dex_token_payload(
    payload: dict[str, Any],
    platform_id: str,
    token_address: str,
    raw_response_id: int | None = None,
) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    if not data:
        return None
    return {
        "platform_id": platform_id,
        "chain_name": data.get("chain_name") or data.get("chain") or data.get("platform"),
        "token_address": token_address,
        "symbol": data.get("symbol"),
        "name": data.get("name"),
        "decimals": _safe_int(data.get("decimals")),
        "project_url": data.get("project_url") or data.get("official_website"),
        "logo": data.get("logo"),
        "raw_response_id": raw_response_id,
    }


# ════════════════════════════════════════════════════════════
# DEX Token 价格 /v1/dex/token/price
# ════════════════════════════════════════════════════════════
def parse_dex_token_price_payload(
    payload: dict[str, Any],
    platform_id: str,
    token_address: str,
    raw_response_id: int | None = None,
) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    if not data:
        return None
    return {
        "platform_id": platform_id,
        "token_address": token_address,
        "snapshot_time": datetime.now(timezone.utc),
        "chain_name": data.get("chain_name") or data.get("chain") or data.get("platform"),
        "price_usd": _safe_float(data.get("price") or data.get("price_usd")),
        "market_cap": _safe_float(data.get("market_cap")),
        "liquidity_usd": _safe_float(data.get("liquidity") or data.get("liquidity_usd")),
        "volume_24h": _safe_float(data.get("volume_24h")),
        "price_change_24h": _safe_float(data.get("price_change_24h")),
        "raw_response_id": raw_response_id,
    }


# ════════════════════════════════════════════════════════════
# DEX Token 池子 /v1/dex/token/pools
# ════════════════════════════════════════════════════════════
def parse_dex_pools_payload(
    payload: dict[str, Any],
    platform_id: str,
    token_address: str,
    raw_response_id: int | None = None,
) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    pools = data.get("pools") or data.get("pool_list") or []
    if isinstance(data, list):
        pools = data

    rows: list[dict[str, Any]] = []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        rows.append(
            {
                "platform_id": platform_id,
                "token_address": token_address,
                "snapshot_time": datetime.now(timezone.utc),
                "pool_address": pool.get("pool_address") or pool.get("address"),
                "dex_name": pool.get("dex_name") or pool.get("dex") or pool.get("name"),
                "pair_name": pool.get("pair_name") or pool.get("pair"),
                "liquidity_usd": _safe_float(pool.get("liquidity") or pool.get("liquidity_usd")),
                "volume_24h": _safe_float(pool.get("volume_24h")),
                "fee_rate": _safe_float(pool.get("fee_rate") or pool.get("fee")),
                "chain_name": pool.get("chain_name") or pool.get("chain"),
                "raw_response_id": raw_response_id,
            }
        )
    return rows


# ════════════════════════════════════════════════════════════
# DEX Token 安全 /v1/dex/security/detail
# ════════════════════════════════════════════════════════════
def parse_dex_security_payload(
    payload: dict[str, Any],
    platform_id: str,
    token_address: str,
    raw_response_id: int | None = None,
) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    if not data:
        return None
    return {
        "platform_id": platform_id,
        "token_address": token_address,
        "snapshot_time": datetime.now(timezone.utc),
        "chain_name": data.get("chain_name") or data.get("chain") or data.get("platform"),
        "is_honeypot": data.get("is_honeypot"),
        "buy_tax": _safe_float(data.get("buy_tax")),
        "sell_tax": _safe_float(data.get("sell_tax")),
        "can_take_back_ownership": data.get("can_take_back_ownership"),
        "owner_address": data.get("owner_address"),
        "risk_level": data.get("risk_level"),
        "security_flags": data.get("security_flags") or data.get("flags") or {},
        "raw_response_id": raw_response_id,
    }