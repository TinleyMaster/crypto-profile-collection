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

    # ── 费用 / 收入（L5 估值层数据源）────────────────────────────────────
    # 2026-10-27 实测：三个 dataType 全部免费、无 key（协议数随上游浮动 ±5）：
    #   /overview/fees?dataType=dailyFees            → 3084 个协议，2139 个 30d > 0
    #   /overview/fees?dataType=dailyRevenue         → 2720 个协议，1711 个 30d > 0
    #   /overview/fees?dataType=dailyHoldersRevenue  → 1250 个协议，  388 个 30d > 0
    # ⚠️ 不带 dataType 时 `total*` 字段是 fees 口径，且 revenue* 字段全为 null
    #    —— 误当收入用会把「手续费规模」当成「协议收入」，差一个量级。
    FEES_DATA_TYPES = ("fees", "revenue", "holders_revenue")
    # 显式映射，不用字符串拼接：holders_revenue 拼不出 dailyHoldersRevenue
    _DL_TYPE_PARAM = {
        "fees": "dailyFees",
        "revenue": "dailyRevenue",
        "holders_revenue": "dailyHoldersRevenue",
    }

    def get_fees_overview(self, data_type: str = "fees") -> dict[str, Any]:
        """Get all protocols' fees/revenue aggregates in one call.

        data_type: fees | revenue | holders_revenue
        Returns {protocols: [{defillamaId, slug, name, category,
                              total24h, total7d, total30d, total1y, ...}], ...}
        """
        if data_type not in self._DL_TYPE_PARAM:
            raise ValueError(f"unsupported data_type: {data_type}")
        return self._get(
            "/overview/fees",
            params={
                "dataType": self._DL_TYPE_PARAM[data_type],
                "excludeTotalDataChart": "true",
                "excludeTotalDataChartBreakdown": "true",
            },
        )

    def get_protocol_fees_history(self, slug: str, data_type: str = "revenue") -> dict[str, Any]:
        """Get one protocol's full daily series for a given data_type.

        实测 aave: totalDataChart 长度 2153（约 5.9 年日线）。
        Returns {total24h, total30d, total1y, totalDataChart: [[unix_ts, value], ...], ...}
        """
        if data_type not in self._DL_TYPE_PARAM:
            raise ValueError(f"unsupported data_type: {data_type}")
        return self._get(
            f"/summary/fees/{slug}",
            params={"dataType": self._DL_TYPE_PARAM[data_type]},
        )

    def get_chains(self) -> list[dict[str, Any]]:
        """Get all chains with latest TVL data.

        Returns list of {name, tvl, symbol, gecko_id, cmcId, ...}.
        """
        return self._get("/v2/chains")

    def get_chain_historical_tvl(self, chain: str) -> list[dict[str, Any]]:
        """Get historical TVL for a single chain (daily resolution).

        Returns list of {date: int (unix ts), tvl: float}.
        Chain name is case-sensitive; use the exact name from get_chains().
        """
        return self._get(f"/v2/historicalChainTvl/{chain}")
