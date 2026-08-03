from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_cmc_info_payload(payload: dict[str, Any], raw_response_id: int | None = None) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    parsed: list[dict[str, Any]] = []

    for cmc_id_str, row in data.items():
        urls = row.get("urls") or {}
        platform_json = row.get("platform") or {}

        tags = row.get("tags") or []
        category_hint = None
        if tags and isinstance(tags, list):
            category_hint = tags[0]

        date_launched = row.get("date_launched")
        if date_launched:
            date_launched = date_launched.split("T", 1)[0]

        parsed.append(
            {
                "cmc_id": int(cmc_id_str),
                "description": row.get("description"),
                "logo": row.get("logo"),
                "notice": row.get("notice"),
                "date_launched": date_launched,
                "tags": tags,
                "urls": urls,
                "platform_json": platform_json,
                "category_hint": category_hint,
                "raw_response_id": raw_response_id,
                "fetched_at": utc_now_iso(),
            }
        )

    return parsed

