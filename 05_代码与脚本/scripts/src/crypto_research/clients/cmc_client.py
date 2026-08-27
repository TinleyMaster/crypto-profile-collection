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

    def get_listings_latest(
        self,
        start: int = 1,
        limit: int = 5000,
        sort: str = "market_cap",
        convert: str = "USD",
    ) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/listings/latest",
            params={
                "start": start,
                "limit": limit,
                "sort": sort,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_cryptocurrency_categories(
        self,
        start: int = 1,
        limit: int = 5000,
    ) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/categories",
            params={"start": start, "limit": limit},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_cryptocurrency_category(
        self,
        category_id: int,
        start: int = 1,
        limit: int = 5000,
        convert: str = "USD",
    ) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/category",
            params={
                "id": category_id,
                "start": start,
                "limit": limit,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_quotes_historical(
        self,
        ids: list[int],
        time_start: str,
        time_end: str | None = None,
        interval: str = "daily",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """获取多个币种的历史行情快照（CMC 专业版 API）。

        Args:
            ids: CMC 币种 ID 列表（单次最多 100 个）
            time_start: 起始时间，ISO 8601 格式，如 "2026-01-01"
            time_end: 结束时间，ISO 8601 格式，默认当前时间
            interval: 采样间隔，"daily" / "hourly" / "5m" 等
            convert: 计价货币

        Returns:
            CMC API 原始响应，data 字段为 {cmc_id: {name, symbol, quotes: [...]}} 结构
        """
        params: dict[str, Any] = {
            "id": ",".join(str(v) for v in ids),
            "time_start": time_start,
            "interval": interval,
            "convert": convert,
        }
        if time_end:
            params["time_end"] = time_end
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v3/cryptocurrency/quotes/historical",
            params=params,
            timeout=self.settings.request_timeout_seconds * 3,  # 历史接口较慢，放宽超时
        )
        response.raise_for_status()
        return response.json()
