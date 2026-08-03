from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crypto_research.config import Settings


class DefiLlamaClient:
    """DefiLlama free API client - no API key required."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        self.session = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": "crypto-research-ingest/1.0",
        }
        self.session.headers.update(headers)

        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.settings.defillama_base_url}{path}"
        response = self.session.get(
            url,
            params=params,
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_protocols(self) -> list[dict[str, Any]]:
        """Get all protocols with TVL data."""
        return self._get("/protocols")

    def get_protocol(self, slug: str) -> dict[str, Any]:
        """Get single protocol detail by slug."""
        return self._get(f"/protocol/{slug}")
