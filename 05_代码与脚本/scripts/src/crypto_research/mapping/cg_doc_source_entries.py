from __future__ import annotations

from typing import Any


def extract_cg_doc_source_entries(
    asset_id: int,
    coin_id: str,
    homepage_url: str | None,
    links: dict | None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    # 1. primary homepage_url
    if homepage_url and homepage_url.strip():
        entries.append(
            {
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": "cg",
                "entry_type": "official_website",
                "entry_url": homepage_url.strip(),
                "discovered_from": "cg_info.homepage_url",
                "is_primary": True,
            }
        )

    if not links:
        return entries

    # 2. links.homepage (additional homepages)
    homepages = links.get("homepage") or []
    if isinstance(homepages, list):
        for url in homepages:
            if not isinstance(url, str) or not url.strip():
                continue
            if homepage_url and url.strip() == homepage_url.strip():
                continue  # skip duplicate
            entries.append(
                {
                    "entity_type": "asset",
                    "asset_id": asset_id,
                    "protocol_id": None,
                    "source_code": "cg",
                    "entry_type": "official_website",
                    "entry_url": url.strip(),
                    "discovered_from": "cg_info.links.homepage",
                    "is_primary": False,
                }
            )

    # 3. blockchain_site
    blockchain_sites = links.get("blockchain_site") or []
    if isinstance(blockchain_sites, list):
        for url in blockchain_sites:
            if not isinstance(url, str) or not url.strip():
                continue
            entries.append(
                {
                    "entity_type": "asset",
                    "asset_id": asset_id,
                    "protocol_id": None,
                    "source_code": "cg",
                    "entry_type": "other",
                    "entry_url": url.strip(),
                    "discovered_from": "cg_info.links.blockchain_site",
                    "is_primary": False,
                }
            )

    # 4. official_forum_url
    forums = links.get("official_forum_url") or []
    if isinstance(forums, list):
        for url in forums:
            if not isinstance(url, str) or not url.strip():
                continue
            entries.append(
                {
                    "entity_type": "asset",
                    "asset_id": asset_id,
                    "protocol_id": None,
                    "source_code": "cg",
                    "entry_type": "other",
                    "entry_url": url.strip(),
                    "discovered_from": "cg_info.links.official_forum_url",
                    "is_primary": False,
                }
            )

    # 5. subreddit_url
    reddit = links.get("subreddit_url")
    if reddit and isinstance(reddit, str) and reddit.strip():
        entries.append(
            {
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": "cg",
                "entry_type": "other",
                "entry_url": reddit.strip(),
                "discovered_from": "cg_info.links.subreddit_url",
                "is_primary": False,
            }
        )

    return entries
