from __future__ import annotations

from typing import Any

from crypto_research.mapping.classify_link import classify_entry_fields


ALLOWED_URL_KEYS = (
    "website",
    "technical_doc",
    "source_code",
    "announcement",
    "twitter",
    "reddit",
    "telegram",
    "facebook",
    "chat",
    "message_board",
    "blog",
    "explorer",
)


def infer_entry_type(url_key: str, url: str) -> str:
    lowered_url = (url or "").lower()
    if url_key == "website":
        return "official_website"
    if url_key == "technical_doc":
        return "docs"
    if url_key == "source_code":
        if "github.com" in lowered_url:
            return "github"
        return "other"
    if url_key == "announcement":
        if "medium.com" in lowered_url:
            return "medium"
        return "other"
    if url_key in ("twitter", "facebook"):
        return url_key
    if url_key == "reddit":
        return "reddit"
    if url_key == "telegram":
        return "telegram"
    if url_key == "blog":
        if "medium.com" in lowered_url:
            return "medium"
        return "other"
    if url_key == "chat":
        return "other"
    if url_key == "message_board":
        return "other"
    if url_key == "explorer":
        return "other"
    return "other"


def extract_doc_source_entries(asset_id: int, cmc_id: int, urls: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for url_key in ALLOWED_URL_KEYS:
        values = urls.get(url_key) or []
        if not isinstance(values, list):
            continue
        for index, url in enumerate(values):
            if not isinstance(url, str):
                continue
            normalized = url.strip()
            if not normalized:
                continue
            topics, method, confidence = classify_entry_fields(
                normalized, source_code="cmc", url_key=url_key
            )
            entries.append(
                {
                    "entity_type": "asset",
                    "asset_id": asset_id,
                    "protocol_id": None,
                    "source_code": "cmc",
                    "entry_type": infer_entry_type(url_key, normalized),
                    "entry_url": normalized,
                    "discovered_from": f"cmc_info.urls.{url_key}",
                    "is_primary": index == 0,
                    "cmc_id": cmc_id,
                    "content_topics": topics,
                    "classify_method": method,
                    "classify_confidence": confidence,
                }
            )

    return entries

