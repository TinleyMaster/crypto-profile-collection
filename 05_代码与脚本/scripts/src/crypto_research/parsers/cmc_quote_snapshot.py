from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


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

        quote_usd = (coin.get("quote") or {}).get("USD") or {}

        rows.append(
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

    return rows
