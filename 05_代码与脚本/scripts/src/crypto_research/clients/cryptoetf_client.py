"""CryptoETF.today API 客户端。

封装 ETF 资金流、CEFI 指数、价格等接口，带重试 + 超时。

支持的资产（13 种）：
    btc, eth, sol, xrp, hyp, doge, link, avax, hbar, ltc, bnb, dot, sui

用法：
    from crypto_research.clients.cryptoetf_client import CryptoETFClient
    client = CryptoETFClient(api_key="xxx")
    summary = client.get_flow_summary()          # 全资产最新一日快照
    sol_history = client.get_asset_flows("sol")  # 单资产历史日频
    cefi = client.get_cefi_index()               # CEFI 综合指数
    prices = client.get_prices()                 # 实时价格
    weekly = client.get_weekly_analytics()       # 周度分析
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# CryptoETF API 支持的全部资产（ticker → 标准大写 symbol）
SUPPORTED_ASSETS: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
    "hyp": "HYPE",
    "doge": "DOGE",
    "link": "LINK",
    "avax": "AVAX",
    "hbar": "HBAR",
    "ltc": "LTC",
    "bnb": "BNB",
    "dot": "DOT",
    "sui": "SUI",
}


class CryptoETFClient:
    """cryptoetf.today API 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.cryptoetf.today/api/v1",
        timeout: int = 15,
    ) -> None:
        if not api_key:
            raise ValueError("CryptoETF API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "crypto-research-etf-client/1.0",
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

    # ── 内部方法 ─────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> Any:
        """发起 GET 请求并返回 JSON。"""
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ── 公开接口 ─────────────────────────────────────────────────

    def get_flow_summary(self) -> dict:
        """获取所有资产的最新一日资金流快照。

        返回结构（示例）：
        {
            "status": "ok",
            "assets": [
                {"symbol": "BTC", "asset": "bitcoin", "netFlowUsdM": 174.6, "date": "2026-09-04"},
                ...
            ]
        }
        """
        return self._get("/flows/summary")

    def get_asset_flows(self, asset: str, days: int = 3650) -> list[dict]:
        """获取单资产的历史日频资金流数据。

        Args:
            asset: 资产代码（btc / eth / sol / ...，大小写不敏感）
            days: 拉取的天数（默认 3650 天 ≈ 10 年，即尽可能拉全部历史）

        Returns:
            按日期升序排列的 dict 列表，每项包含 date、netFlowUsdM 等字段。
            字段以 API 实际返回为准（目前已知：date, netFlowUsdM）。
        """
        asset_lower = asset.lower().strip()
        params = {"days": days}
        data = self._get(f"/flows/{asset_lower}", params=params)

        # API 返回结构：{"symbol": "SOL", "asset": "solana", "windowDays": 30,
        #               "days": [{"date": "2026-08-09", "netFlowUsdM": 0}, ...],
        #               "updatedAt": "..."}
        if isinstance(data, dict):
            days_list = data.get("days")
            if isinstance(days_list, list):
                return days_list
            # 兼容其他可能的 key
            for key in ("data", "flows", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    def get_cefi_index(self) -> dict:
        """获取 CEFI 综合机构情绪指数。"""
        return self._get("/index/cefi")

    def get_prices(self) -> dict:
        """获取所有追踪资产的实时价格和 24h 涨跌幅。"""
        return self._get("/prices")

    def get_tickers(self) -> list[dict]:
        """获取所有支持的资产目录（symbol / id / name）。"""
        data = self._get("/tickers")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "tickers", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def get_weekly_analytics(self) -> dict:
        """获取周度资金流 + AUM 分析数据。"""
        return self._get("/analytics/weekly")
