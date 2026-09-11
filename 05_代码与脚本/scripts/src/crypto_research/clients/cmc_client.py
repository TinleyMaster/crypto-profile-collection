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
            status_forcelist=[500, 502, 503, 504],   # 429 不在此：交脚本层做指数退避 + Retry-After
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _check_api_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验 CMC 响应体内的业务状态码（HTTP 200 但 body.error_code != 0 时抛错）。

        CMC 部分接口（如 fear-and-greed / altcoin-season-index）在高负载时返回
        HTTP 200 + {"status": {"error_code": "500", "error_message": "..."}}，
        仅靠 raise_for_status() 无法捕获，这里统一校验。
        """
        status = payload.get("status") or {}
        error_code = status.get("error_code")
        if error_code is not None and int(error_code) != 0:
            message = status.get("error_message") or status.get("notice") or "unknown error"
            raise requests.HTTPError(f"CMC API status error {error_code}: {message}")
        return payload

    # ═══ 加密货币基础数据 ═══

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
        sort_dir: str = "desc",
        convert: str = "USD",
        price_min: float | None = None,
        price_max: float | None = None,
        market_cap_min: float | None = None,
        market_cap_max: float | None = None,
        volume_24h_min: float | None = None,
        volume_24h_max: float | None = None,
        circulating_supply_min: float | None = None,
        circulating_supply_max: float | None = None,
        percent_change_24h_min: float | None = None,
        percent_change_24h_max: float | None = None,
        tag: str | None = None,
        cryptocurrency_type: str | None = None,
        aux: str | None = None,
    ) -> dict[str, Any]:
        """获取市场行情列表（v3 版本，支持过滤参数）。

        相比 v1，v3 在响应中把 quote 从对象改为数组（quote[0] 为默认计价币行情）。
        过滤参数可在 API 侧完成长尾筛选 / 赛道筛选 / 涨跌幅筛选，减少本地全表过滤。
        """
        params: dict[str, Any] = {
            "start": start,
            "limit": limit,
            "sort": sort,
            "sort_dir": sort_dir,
            "convert": convert,
        }
        filters = {
            "price_min": price_min,
            "price_max": price_max,
            "market_cap_min": market_cap_min,
            "market_cap_max": market_cap_max,
            "volume_24h_min": volume_24h_min,
            "volume_24h_max": volume_24h_max,
            "circulating_supply_min": circulating_supply_min,
            "circulating_supply_max": circulating_supply_max,
            "percent_change_24h_min": percent_change_24h_min,
            "percent_change_24h_max": percent_change_24h_max,
            "tag": tag,
            "cryptocurrency_type": cryptocurrency_type,
            "aux": aux,
        }
        for key, value in filters.items():
            if value is not None:
                params[key] = value
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v3/cryptocurrency/listings/latest",
            params=params,
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_listings_new(
        self,
        start: int = 1,
        limit: int = 100,
        convert: str = "USD",
        sort_dir: str = "desc",
    ) -> dict[str, Any]:
        """获取最近新上市的加密货币列表。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/listings/new",
            params={
                "start": start,
                "limit": limit,
                "convert": convert,
                "sort_dir": sort_dir,
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
        category_id: str,
        start: int = 1,
        limit: int = 100,
        convert: str = "USD",
    ) -> dict[str, Any]:
        # 注意：CMC 单分类成员端点会拒绝 limit=5000（实测 400），limit=100 已确认可用，
        # 大分类靠调用方分页（start 递增）取全。
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

    # ═══ 宏观指标（全球市值 / 恐贪 / 山寨季）═══

    def get_global_metrics(self, convert: str = "USD") -> dict[str, Any]:
        """全球加密货币市场指标。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/global-metrics/quotes/latest",
            params={"convert": convert},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return self._check_api_status(response.json())

    def get_fear_greed(self) -> dict[str, Any]:
        """恐贪指数（最新值）。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v3/fear-and-greed",
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return self._check_api_status(response.json())

    def get_fear_greed_history(self, days: int = 365) -> dict[str, Any]:
        """恐贪指数历史序列。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v3/fear-and-greed",
            params={"limit": days},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return self._check_api_status(response.json())

    def get_altcoin_season(self) -> dict[str, Any]:
        """山寨季指数。需走 /trial-pro-api/ 前缀（该接口未在标准 pro-api 路径暴露）。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/trial-pro-api/v1/altcoin-season-index",
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return self._check_api_status(response.json())

    # ═══ 趋势榜 ═══

    def get_trending_latest(
        self,
        start: int = 1,
        limit: int = 100,
        time_period: str = "24h",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """按 CMC 搜索量排名的热门币。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/trending/latest",
            params={
                "start": start,
                "limit": limit,
                "time_period": time_period,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_trending_gainers_losers(
        self,
        start: int = 1,
        limit: int = 100,
        time_period: str = "24h",
        sort_dir: str = "desc",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """按涨跌幅排名的榜单（涨家 / 跌家）。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/trending/gainers-losers",
            params={
                "start": start,
                "limit": limit,
                "time_period": time_period,
                "sort": "percent_change_24h",
                "sort_dir": sort_dir,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_trending_most_visited(
        self,
        start: int = 1,
        limit: int = 100,
        time_period: str = "24h",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """按访问量排名的热门币。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/trending/most-visited",
            params={
                "start": start,
                "limit": limit,
                "time_period": time_period,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ═══ 空投 ═══

    def get_airdrops(
        self,
        start: int = 1,
        limit: int = 100,
        status: str = "ONGOING",
    ) -> dict[str, Any]:
        """空投活动列表。status: ENDED / ONGOING / UPCOMING。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/airdrops",
            params={"start": start, "limit": limit, "status": status},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_airdrop(self, airdrop_id: str) -> dict[str, Any]:
        """单个空投活动详情。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/cryptocurrency/airdrop",
            params={"id": airdrop_id},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ═══ OHLCV（K线）═══

    def get_ohlcv_latest(
        self,
        ids: list[int],
        convert: str = "USD",
    ) -> dict[str, Any]:
        """当日 OHLCV。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v2/cryptocurrency/ohlcv/latest",
            params={
                "id": ",".join(str(v) for v in ids),
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_ohlcv_historical(
        self,
        ids: list[int],
        time_period: str = "daily",
        time_start: str | None = None,
        time_end: str | None = None,
        count: int = 365,
        interval: str = "daily",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """历史 OHLCV。"""
        params: dict[str, Any] = {
            "id": ",".join(str(v) for v in ids),
            "time_period": time_period,
            "count": count,
            "interval": interval,
            "convert": convert,
        }
        if time_start:
            params["time_start"] = time_start
        if time_end:
            params["time_end"] = time_end
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v2/cryptocurrency/ohlcv/historical",
            params=params,
            timeout=self.settings.request_timeout_seconds * 3,
        )
        response.raise_for_status()
        return response.json()

    # ═══ 价格表现统计（ATH/ATL）═══

    def get_price_performance_stats(
        self,
        ids: list[int],
        time_period: str = "all_time",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """价格表现统计：ATH/ATL、发行价 ROI、多周期涨跌幅。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v2/cryptocurrency/price-performance-stats/latest",
            params={
                "id": ",".join(str(v) for v in ids),
                "time_period": time_period,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ═══ 交易对快照 ═══

    def get_market_pairs(
        self,
        ids: list[int],
        start: int = 1,
        limit: int = 100,
        sort: str = "volume_24h_strict",
        sort_dir: str = "desc",
        category: str = "all",
        convert: str = "USD",
    ) -> dict[str, Any]:
        """列出代币在所有交易所的交易对。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v2/cryptocurrency/market-pairs/latest",
            params={
                "id": ",".join(str(v) for v in ids),
                "start": start,
                "limit": limit,
                "sort": sort,
                "sort_dir": sort_dir,
                "category": category,
                "convert": convert,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ═══ DEX Token ═══

    def get_dex_token(
        self,
        platform_id: str,
        token_address: str,
    ) -> dict[str, Any]:
        """DEX Token 详情。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/dex/token",
            params={"platform-id": platform_id, "address": token_address},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_dex_token_price(
        self,
        platform_id: str,
        token_address: str,
    ) -> dict[str, Any]:
        """DEX Token 最新价格。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/dex/token/price",
            params={"platform-id": platform_id, "address": token_address},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_dex_token_pools(
        self,
        platform_id: str,
        token_address: str,
    ) -> dict[str, Any]:
        """DEX Token 流动性池列表。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/dex/token/pools",
            params={"platform-id": platform_id, "address": token_address},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_dex_security(
        self,
        platform_id: str,
        token_address: str,
    ) -> dict[str, Any]:
        """DEX Token 安全/风险详情。"""
        response = self.session.get(
            f"{self.settings.cmc_base_url}/v1/dex/security/detail",
            params={"platform-id": platform_id, "address": token_address},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()