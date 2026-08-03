from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes"}:
            return True
        if normalized in {"0", "false", "f", "no"}:
            return False
    return None


def parse_cmc_map_payload(payload: dict[str, Any], raw_response_id: int | None = None) -> list[dict[str, Any]]:
    rows = payload.get("data") or []
    parsed: list[dict[str, Any]] = []

    for row in rows:
        cmc_id = row.get("id")
        if not cmc_id:
            continue

        platform = row.get("platform") or {}

        parsed.append(
            {
                "cmc_id": cmc_id,
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "slug": row.get("slug"),
                "listing_status": "active",
                "is_active": to_optional_bool(row.get("is_active")),
                "rank_num": row.get("rank"),
                "platform_name": platform.get("name"),
                "platform_slug": platform.get("slug"),
                "platform_symbol": platform.get("symbol"),
                "token_address": platform.get("token_address"),
                "first_historical_data": row.get("first_historical_data"),
                "last_historical_data": row.get("last_historical_data"),
                "raw_response_id": raw_response_id,
                "fetched_at": utc_now_iso(),
            }
        )

    return parsed
