from __future__ import annotations

import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crypto_research.config import Settings


class CoinGeckoClient:
    def __init__(self, settings: Settings, calls_per_minute: int = 90) -> None:
        self.settings = settings
        self._min_interval = 60.0 / calls_per_minute
        self._last_call: float = 0.0

        self.session = requests.Session()
        headers = {
            "Accept": "application/json",
            "User-Agent": "crypto-research-ingest/1.0",
        }
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
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

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._rate_limit()
        url = f"{self.settings.coingecko_base_url}{path}"
        response = self.session.get(
            url,
            params=params,
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_coins_list(self, include_platform: bool = False) -> list[dict[str, Any]]:
        return self._get(
            "/coins/list",
            params={"include_platform": str(include_platform).lower()},
        )

    def get_coin_by_id(
        self,
        coin_id: str,
        localization: bool = False,
        tickers: bool = False,
        community_data: bool = False,
        developer_data: bool = False,
    ) -> dict[str, Any]:
        return self._get(
            f"/coins/{coin_id}",
            params={
                "localization": str(localization).lower(),
                "tickers": str(tickers).lower(),
                "community_data": str(community_data).lower(),
                "developer_data": str(developer_data).lower(),
            },
        )
