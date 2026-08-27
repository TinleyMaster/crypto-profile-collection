from __future__ import annotations

from typing import Any

from crypto_research.mapping.classify_link import classify_entry_fields


def _classify(url: str) -> dict[str, Any]:
    topics, method, confidence = classify_entry_fields(url, source_code="cg")
    return {
        "content_topics": topics,
        "classify_method": method,
        "classify_confidence": confidence,
    }


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
                "is_primary": False,  # 统一由裁决脚本设置，避免多来源各标各的
                **_classify(homepage_url.strip()),
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
                    **_classify(url.strip()),
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
                    **_classify(url.strip()),
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
                    **_classify(url.strip()),
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
                "entry_type": "reddit",
                "entry_url": reddit.strip(),
                "discovered_from": "cg_info.links.subreddit_url",
                "is_primary": False,
                **_classify(reddit.strip()),
            }
        )

    # 6. twitter_screen_name → 构造 Twitter URL
    twitter_sn = links.get("twitter_screen_name")
    if twitter_sn and isinstance(twitter_sn, str) and twitter_sn.strip():
        entries.append(
            {
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": "cg",
                "entry_type": "twitter",
                "entry_url": f"https://x.com/{twitter_sn.strip()}",
                "discovered_from": "cg_info.links.twitter_screen_name",
                "is_primary": False,
                **_classify(f"https://x.com/{twitter_sn.strip()}"),
            }
        )

    # 7. telegram_channel_identifier → 构造 Telegram URL
    tg_id = links.get("telegram_channel_identifier")
    if tg_id and isinstance(tg_id, str) and tg_id.strip():
        entries.append(
            {
                "entity_type": "asset",
                "asset_id": asset_id,
                "protocol_id": None,
                "source_code": "cg",
                "entry_type": "telegram",
                "entry_url": f"https://t.me/{tg_id.strip()}",
                "discovered_from": "cg_info.links.telegram_channel_identifier",
                "is_primary": False,
                **_classify(f"https://t.me/{tg_id.strip()}"),
            }
        )

    # 8. repos_url (GitHub 等代码仓库)
    repos = links.get("repos_url") or {}
    if isinstance(repos, dict):
        gh_repos = repos.get("github") or []
        if isinstance(gh_repos, list):
            for url in gh_repos:
                if not isinstance(url, str) or not url.strip():
                    continue
                entries.append(
                    {
                        "entity_type": "asset",
                        "asset_id": asset_id,
                        "protocol_id": None,
                        "source_code": "cg",
                        "entry_type": "github",
                        "entry_url": url.strip(),
                        "discovered_from": "cg_info.links.repos_url.github",
                        "is_primary": False,
                        **_classify(url.strip()),
                    }
                )
        # bitbucket repos
        bb_repos = repos.get("bitbucket") or []
        if isinstance(bb_repos, list):
            for url in bb_repos:
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
                        "discovered_from": "cg_info.links.repos_url.bitbucket",
                        "is_primary": False,
                        **_classify(url.strip()),
                    }
                )

    # 9. announcement_url
    announcements = links.get("announcement_url") or []
    if isinstance(announcements, list):
        for url in announcements:
            if not isinstance(url, str) or not url.strip():
                continue
            lowered = url.strip().lower()
            a_type = "medium" if "medium.com" in lowered else "other"
            entries.append(
                {
                    "entity_type": "asset",
                    "asset_id": asset_id,
                    "protocol_id": None,
                    "source_code": "cg",
                    "entry_type": a_type,
                    "entry_url": url.strip(),
                    "discovered_from": "cg_info.links.announcement_url",
                    "is_primary": False,
                    **_classify(url.strip()),
                }
            )

    return entries
