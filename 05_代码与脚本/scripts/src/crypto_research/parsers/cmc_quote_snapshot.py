from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# 上游 CMC 对低流动性/未上所/已下架资产把「无数据」编码成 0，与真值 0 不可分辨。
# 按项目铁律（0 与缺失不可分辨时一律按无数据）统一归一化为 NULL，
# 避免伪 0 污染市值/成交量统计（审计 P0-2，2026-09-18）。
_ZERO_AS_NULL_FIELDS = ("market_cap", "fdv", "volume_24h")


def normalize_zero_as_null(row: dict[str, Any]) -> dict[str, Any]:
    """把 CMC 返回的占位 0 归一化为 NULL（原地修改并返回）。

    - ``market_cap`` / ``fdv`` / ``volume_24h`` == 0 → NULL
    - ``price_usd`` 缺失或 <= 0 时，上述市值/成交量字段同样置 NULL
      （没有可信价格时，基于价格的市值/成交量无意义）
    """
    price = row.get("price_usd")
    try:
        price_missing = price is None or float(price) <= 0
    except (TypeError, ValueError):
        price_missing = True

    for field in _ZERO_AS_NULL_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        try:
            if float(value) == 0:
                row[field] = None
        except (TypeError, ValueError):
            row[field] = None

    if price_missing:
        for field in _ZERO_AS_NULL_FIELDS:
            row[field] = None

    return row


def parse_cmc_quote_snapshot_payload(
    payload: dict[str, Any],
    raw_response_id: int | None = None,
) -> list[dict[str, Any]]:
    """Parse CMC /v1/cryptocurrency/listings/latest response into quote snapshot rows.

    Returns list of dicts with keys matching src_cmc.cmc_asset_quote_snapshot columns.
    """
    data = payload.get("data") or []
    status = payload.get("status") or {}
    # Use server timestamp from response status if available
    quote_time_str = status.get("timestamp")
    if quote_time_str:
        quote_time = datetime.fromisoformat(quote_time_str.replace("Z", "+00:00"))
    else:
        quote_time = datetime.now(timezone.utc)

    rows: list[dict[str, Any]] = []
    for coin in data:
        cmc_id = coin.get("id")
        if cmc_id is None:
            continue

        # v1: quote 为 { "USD": {...} } 对象；v3: quote 为 [ {...} ] 数组（默认计价币在首位）
        quote = coin.get("quote") or {}
        if isinstance(quote, list):
            quote_usd = quote[0] if quote else {}
        else:
            quote_usd = quote.get("USD") or {}

        rows.append(
            normalize_zero_as_null(
                {
                    "cmc_id": cmc_id,
                    "quote_time": quote_time,
                    "price_usd": quote_usd.get("price"),
                    "market_cap": quote_usd.get("market_cap"),
                    "fdv": quote_usd.get("fully_diluted_market_cap"),
                    "volume_24h": quote_usd.get("volume_24h"),
                    "circulating_supply": coin.get("circulating_supply"),
                    "total_supply": coin.get("total_supply"),
                    "max_supply": coin.get("max_supply"),
                    "percent_change_1h": quote_usd.get("percent_change_1h"),
                    "percent_change_24h": quote_usd.get("percent_change_24h"),
                    "percent_change_7d": quote_usd.get("percent_change_7d"),
                    "percent_change_30d": quote_usd.get("percent_change_30d"),
                    "market_cap_dominance": quote_usd.get("market_cap_dominance"),
                    "raw_response_id": raw_response_id,
                }
            )
        )

    return rows
