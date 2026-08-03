from __future__ import annotations

from typing import Any


def parse_cg_coin_list_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "coin_id": entry["id"],
        "symbol": entry["symbol"],
        "name": entry["name"],
        "platforms": entry.get("platforms"),
    }
