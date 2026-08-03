from __future__ import annotations

from typing import Any


def parse_dl_protocol_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_id": str(entry.get("id", "")),
        "name": entry.get("name", ""),
        "symbol": (entry.get("symbol") or "").upper(),
        "slug": entry.get("slug", ""),
        "category": entry.get("category"),
        "chain": entry.get("chain"),
        "chains": entry.get("chains", []),
        "tvl": _safe_num(entry.get("tvl")),
        "change_1h": _safe_num(entry.get("change_1h")),
        "change_1d": _safe_num(entry.get("change_1d")),
        "change_7d": _safe_num(entry.get("change_7d")),
        "url": entry.get("url"),
        "description": _clean_text(entry.get("description")),
        "address": entry.get("address"),
        "twitter": entry.get("twitter"),
        "cmc_id": str(entry["cmcId"]) if entry.get("cmcId") else None,
        "gecko_id": entry.get("gecko_id"),
    }


def _safe_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = " ".join(text.split()).strip()
    return cleaned or None
