from __future__ import annotations

import itertools
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

        # 多 key 轮替
        if settings.coingecko_api_keys:
            self._keys = settings.coingecko_api_keys
            self._key_cycle = itertools.cycle(self._keys)
            self._current_key = next(self._key_cycle)
        elif settings.coingecko_api_key:
            self._keys = [settings.coingecko_api_key]
            self._key_cycle = None
            self._current_key = settings.coingecko_api_key
        else:
            self._keys = []
            self._key_cycle = None
            self._current_key = None

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

    def _rotate_key(self) -> None:
        """切换到下一个 API Key。"""
        if self._key_cycle is not None:
            self._current_key = next(self._key_cycle)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._rate_limit()
        url = f"{self.settings.coingecko_base_url}{path}"
        headers = self.session.headers.copy()
        if self._current_key:
            headers["x-cg-demo-api-key"] = self._current_key
        response = self.session.get(
            url,
            params=params,
            headers=headers,
            timeout=self.settings.request_timeout_seconds,
        )
        # 429 时切换到下一个 key 并重试一次
        if response.status_code == 429 and self._key_cycle is not None:
            self._rotate_key()
            print(f"  [CG] 429 限流，切换到下一个 API Key")
            self._rate_limit()
            headers = self.session.headers.copy()
            if self._current_key:
                headers["x-cg-demo-api-key"] = self._current_key
            response = self.session.get(
                url,
                params=params,
                headers=headers,
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
