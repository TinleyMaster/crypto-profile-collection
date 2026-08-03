from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crypto_research.config import Settings


class CMCClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "X-CMC_PRO_API_KEY": settings.cmc_api_key,
                "Accept": "application/json",
                "User-Agent": "crypto-research-ingest/1.0",
            }
        )
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get_cryptocurrency_map(
        self, listing_status: str = "active", sort: str = "cmc_rank"
    ) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/map",
            params={
                "listing_status": listing_status,
                "sort": sort,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_cryptocurrency_info(self, ids: list[int]) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v2/cryptocurrency/info",
            params={
                "id": ",".join(str(value) for value in ids),
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
