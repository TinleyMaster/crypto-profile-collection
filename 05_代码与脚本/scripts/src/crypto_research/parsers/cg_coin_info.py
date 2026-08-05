from __future__ import annotations

from typing import Any


def parse_cg_coin_info_payload(
    payload: dict[str, Any],
    raw_response_id: int | None = None,
) -> dict[str, Any]:
    links = payload.get("links", {}) or {}

    return {
        "coin_id": payload["id"],
        "symbol": (payload.get("symbol") or "").upper(),
        "name": payload.get("name"),
        "description": _clean_description(payload.get("description", {})),
        "homepage_url": _first_valid_url(links.get("homepage", [])),
        "image": payload.get("image", {}).get("large"),
        "genesis_date": payload.get("genesis_date"),
        "market_cap_rank": payload.get("market_cap_rank"),
        "coingecko_rank": payload.get("coingecko_rank"),
        "categories": payload.get("categories", []),
        "platforms": payload.get("platforms", {}),
        "links": {
            "homepage": links.get("homepage", []),
            "blockchain_site": links.get("blockchain_site", []),
            "official_forum_url": links.get("official_forum_url", []),
            "twitter_screen_name": links.get("twitter_screen_name"),
            "telegram_channel_identifier": links.get("telegram_channel_identifier"),
            "subreddit_url": links.get("subreddit_url"),
            "repos_url": links.get("repos_url", {}),
            "announcement_url": links.get("announcement_url", []),
        },
        "raw_response_id": raw_response_id,
    }


def _clean_description(desc: dict[str, str] | None) -> str | None:
    if not desc:
        return None
    text = desc.get("en", "") or ""
    text = " ".join(text.split()).strip()
    return text or None


def _first_valid_url(urls: list[str] | None) -> str | None:
    if not urls:
        return None
    for u in urls:
        if u and u.strip():
            return u.strip()
    return None
