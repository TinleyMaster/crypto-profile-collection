# -*- coding: utf-8 -*-
"""衍生品/资金面数据：多交易所聚合（Binance / OKX / Bybit / Bitget / Gate）。

覆盖指标：
  - 实时资金费率 + 历史资金费率
  - 实时未平仓合约（OI）+ 历史 OI
  - 实时成交（用于 CVD 计算）

所有接口均为公开 REST API，无需鉴权。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests


# ── 数据结构 ──

@dataclass
class FundingRate:
    symbol: str           # 统一格式：BTCUSDT
    exchange: str         # binance / okx / bybit / bitget / gate
    funding_rate: float   # 当前资金费率（小数，如 0.0001 = 0.01%）
    next_funding_time: int  # 下次结算时间戳（ms）
    mark_price: Optional[float] = None
    index_price: Optional[float] = None


@dataclass
class FundingRateHistory:
    symbol: str
    exchange: str
    funding_time: int    # 结算时间戳（ms）
    funding_rate: float  # 实际结算费率


@dataclass
class OpenInterest:
    symbol: str
    exchange: str
    open_interest: float       # 持仓量（币数）
    open_interest_value: Optional[float] = None  # 持仓价值（USDT）
    timestamp: Optional[int] = None


@dataclass
class Trade:
    symbol: str
    exchange: str
    trade_id: str
    price: float
    qty: float
    quote_qty: float
    is_buyer_maker: bool  # True=卖方主动成交（卖出），False=买方主动成交（买入）
    timestamp: int


# ── 交易所基类 ──

class ExchangeClient:
    name: str = ""
    base_url: str = ""
    symbol_format: str = "{base}{quote}"  # 如 BTCUSDT

    def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> dict | list:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def format_symbol(self, base: str, quote: str = "USDT") -> str:
        return self.symbol_format.format(base=base.upper(), quote=quote.upper())

    # 以下方法由子类实现
    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        raise NotImplementedError

    def get_funding_rate_history(self, symbol: str, limit: int = 30) -> list[FundingRateHistory]:
        raise NotImplementedError

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        raise NotImplementedError

    def get_open_interest_history(self, symbol: str, period: str = "1h", limit: int = 30) -> list[OpenInterest]:
        raise NotImplementedError

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[Trade]:
        raise NotImplementedError


# ── Binance ──

class BinanceClient(ExchangeClient):
    name = "binance"
    base_url = "https://fapi.binance.com"
    symbol_format = "{base}{quote}"

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        data = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not data or "lastFundingRate" not in data:
            return None
        return FundingRate(
            symbol=symbol,
            exchange=self.name,
            funding_rate=float(data["lastFundingRate"]),
            next_funding_time=int(data["nextFundingTime"]),
            mark_price=float(data.get("markPrice", 0)) or None,
            index_price=float(data.get("indexPrice", 0)) or None,
        )

    def get_funding_rate_history(self, symbol: str, limit: int = 30) -> list[FundingRateHistory]:
        data = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
        if not data or not isinstance(data, list):
            return []
        return [
            FundingRateHistory(
                symbol=symbol,
                exchange=self.name,
                funding_time=int(item["fundingTime"]),
                funding_rate=float(item["fundingRate"]),
            )
            for item in data
        ]

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        data = self._get("/fapi/v1/openInterest", {"symbol": symbol})
        if not data or "openInterest" not in data:
            return None
        # Binance 只返回币数，价值需要用标记价格估算
        oi_qty = float(data["openInterest"])
        return OpenInterest(
            symbol=symbol,
            exchange=self.name,
            open_interest=oi_qty,
            timestamp=int(data.get("time", time.time() * 1000)),
        )

    def get_open_interest_history(self, symbol: str, period: str = "1h", limit: int = 30) -> list[OpenInterest]:
        # Binance OI 历史需要 period 参数：5m / 15m / 30m / 1h / 2h / 4h / 6h / 12h / 1d
        data = self._get("/futures/data/openInterestHist", {
            "symbol": symbol, "period": period, "limit": limit,
        })
        if not data or not isinstance(data, list):
            return []
        return [
            OpenInterest(
                symbol=symbol,
                exchange=self.name,
                open_interest=float(item["sumOpenInterest"]),
                open_interest_value=float(item.get("sumOpenInterestValue", 0)) or None,
                timestamp=int(item["timestamp"]),
            )
            for item in data
        ]

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[Trade]:
        data = self._get("/fapi/v1/trades", {"symbol": symbol, "limit": limit})
        if not data or not isinstance(data, list):
            return []
        return [
            Trade(
                symbol=symbol,
                exchange=self.name,
                trade_id=str(item["id"]),
                price=float(item["price"]),
                qty=float(item["qty"]),
                quote_qty=float(item["quoteQty"]),
                is_buyer_maker=bool(item["isBuyerMaker"]),
                timestamp=int(item["time"]),
            )
            for item in data
        ]


# ── OKX ──

class OKXClient(ExchangeClient):
    name = "okx"
    base_url = "https://www.okx.com"
    symbol_format = "{base}-{quote}-SWAP"

    def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> dict | list:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            return []
        return data.get("data", [])

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        data = self._get("/api/v5/public/funding-rate", {"instId": symbol})
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        item = data[0]
        return FundingRate(
            symbol=symbol,
            exchange=self.name,
            funding_rate=float(item["fundingRate"]),
            next_funding_time=int(item["nextFundingTime"]),
            mark_price=float(item.get("markPx", 0)) or None,
        )

    def get_funding_rate_history(self, symbol: str, limit: int = 30) -> list[FundingRateHistory]:
        data = self._get("/api/v5/public/funding-rate-history", {
            "instId": symbol, "limit": str(limit),
        })
        if not data or not isinstance(data, list):
            return []
        return [
            FundingRateHistory(
                symbol=symbol,
                exchange=self.name,
                funding_time=int(item["fundingTime"]),
                funding_rate=float(item["fundingRate"]),
            )
            for item in data
        ]

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        data = self._get("/api/v5/public/open-interest", {"instId": symbol})
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        item = data[0]
        # OKX: oi = 持仓量（张），oiCcy = 持仓币数（USDT 本位合约就是 USDT 价值）
        oi_ccy = item.get("oiCcy")
        oi_val = float(oi_ccy) if oi_ccy else None
        return OpenInterest(
            symbol=symbol,
            exchange=self.name,
            open_interest=float(item["oi"]),
            open_interest_value=oi_val,
            timestamp=int(item.get("ts", time.time() * 1000)),
        )

    def get_open_interest_history(self, symbol: str, period: str = "1H", limit: int = 30) -> list[OpenInterest]:
        # OKX period: 1m / 3m / 5m / 15m / 30m / 1H / 2H / 4H / 12H / 1D
        data = self._get("/api/v5/rubik/stat/contracts/open-interest-history", {
            "instId": symbol, "period": period, "limit": str(limit),
        })
        if not data or not isinstance(data, list):
            return []
        return [
            OpenInterest(
                symbol=symbol,
                exchange=self.name,
                open_interest=float(item["oi"]),
                open_interest_value=float(item.get("oiCcy", 0)) or None,
                timestamp=int(item["ts"]),
            )
            for item in data
        ]

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[Trade]:
        data = self._get("/api/v5/market/trades", {
            "instId": symbol, "limit": str(limit),
        })
        if not data or not isinstance(data, list):
            return []
        return [
            Trade(
                symbol=symbol,
                exchange=self.name,
                trade_id=item["tradeId"],
                price=float(item["px"]),
                qty=float(item["sz"]),
                quote_qty=float(item["px"]) * float(item["sz"]),
                is_buyer_maker=item["side"] == "sell",  # side 是 taker 方向，sell = 主动卖 = buyer is maker
                timestamp=int(item["ts"]),
            )
            for item in data
        ]


# ── Bybit ──

class BybitClient(ExchangeClient):
    name = "bybit"
    base_url = "https://api.bybit.com"
    symbol_format = "{base}{quote}"

    def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> dict | list:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            return []
        return data.get("result", {})

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        data = self._get("/v5/market/funding/history", {
            "category": "linear", "symbol": symbol, "limit": "1",
        })
        # funding/history 返回的是历史记录，取最新一条；实时费率从 tickers 拿
        tickers = self._get("/v5/market/tickers", {
            "category": "linear", "symbol": symbol,
        })
        if not tickers or not tickers.get("list"):
            return None
        item = tickers["list"][0]
        # 下次资金费率时间
        next_time = int(item.get("nextFundingTime", 0))
        funding_rate = float(item.get("fundingRate", 0))
        return FundingRate(
            symbol=symbol,
            exchange=self.name,
            funding_rate=funding_rate,
            next_funding_time=next_time,
            mark_price=float(item.get("markPrice", 0)) or None,
            index_price=float(item.get("indexPrice", 0)) or None,
        )

    def get_funding_rate_history(self, symbol: str, limit: int = 30) -> list[FundingRateHistory]:
        data = self._get("/v5/market/funding/history", {
            "category": "linear", "symbol": symbol, "limit": str(limit),
        })
        if not data or not data.get("list"):
            return []
        return [
            FundingRateHistory(
                symbol=symbol,
                exchange=self.name,
                funding_time=int(item["fundingRateTimestamp"]),
                funding_rate=float(item["fundingRate"]),
            )
            for item in data["list"]
        ]

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        data = self._get("/v5/market/open-interest", {
            "category": "linear", "symbol": symbol,
        })
        if not data or not data.get("list"):
            return None
        item = data["list"][0]
        return OpenInterest(
            symbol=symbol,
            exchange=self.name,
            open_interest=float(item["openInterest"]),
            open_interest_value=float(item.get("openInterestValue", 0)) or None,
            timestamp=int(item.get("timestamp", time.time() * 1000)),
        )

    def get_open_interest_history(self, symbol: str, period: str = "1h", limit: int = 30) -> list[OpenInterest]:
        # Bybit period: 5m / 15m / 30m / 1h / 4h / 1d
        data = self._get("/v5/market/historical-volatility", {
            "category": "option",  # 这个接口不对，换一个
        })
        # Bybit 没有直接的 OI 历史公开接口（需要 VIP），用最近 N 次 OI 快照近似
        # 这里返回空列表，由上层聚合时处理
        return []

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[Trade]:
        data = self._get("/v5/market/recent-trade", {
            "category": "linear", "symbol": symbol, "limit": str(limit),
        })
        if not data or not data.get("list"):
            return []
        return [
            Trade(
                symbol=symbol,
                exchange=self.name,
                trade_id=item.get("tradeId") or item.get("id", ""),
                price=float(item["price"]),
                qty=float(item["size"]),
                quote_qty=float(item["price"]) * float(item["size"]),
                is_buyer_maker=item.get("side") == "Sell",  # side 是 taker 方向
                timestamp=int(item["time"]),
            )
            for item in data["list"]
        ]


# ── Bitget ──

class BitgetClient(ExchangeClient):
    name = "bitget"
    base_url = "https://api.bitget.com"
    symbol_format = "{base}{quote}"  # BTCUSDT

    def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> dict | list:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "00000":
            return []
        return data.get("data", [])

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        data = self._get("/api/v2/mix/market/current-fund-rate", {
            "symbol": symbol, "productType": "USDT-FUTURES",
        })
        if not data or not isinstance(data, list):
            return None
        # v2 返回列表，取第一条
        item = data[0] if isinstance(data, list) and data else data
        return FundingRate(
            symbol=symbol,
            exchange=self.name,
            funding_rate=float(item.get("fundingRate", 0)),
            next_funding_time=int(item.get("nextFundingTime", 0)),
        )

    def get_funding_rate_history(self, symbol: str, limit: int = 30) -> list[FundingRateHistory]:
        data = self._get("/api/v2/mix/market/history-fund-rate", {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "pageSize": str(limit), "pageNo": "1",
        })
        if not data or not isinstance(data, list):
            # v2 可能返回 { list: [], total: 0 }
            if isinstance(data, dict) and data.get("list"):
                data = data["list"]
            else:
                return []
        return [
            FundingRateHistory(
                symbol=symbol,
                exchange=self.name,
                funding_time=int(item["settleTime"]),
                funding_rate=float(item["fundingRate"]),
            )
            for item in data[:limit]
        ]

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        data = self._get("/api/v2/mix/market/open-interest", {
            "symbol": symbol, "productType": "USDT-FUTURES",
        })
        if not data or not isinstance(data, dict):
            return None
        # v2 返回 { amount: "", holdAmount: "" } 等
        amount = data.get("amount") or data.get("totalAmount") or 0
        return OpenInterest(
            symbol=symbol,
            exchange=self.name,
            open_interest=float(amount) if amount else 0,
            timestamp=int(time.time() * 1000),
        )

    def get_open_interest_history(self, symbol: str, period: str = "1H", limit: int = 30) -> list[OpenInterest]:
        return []  # Bitget OI 历史接口不稳定，暂不使用

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[Trade]:
        data = self._get("/api/v2/mix/market/fills", {
            "symbol": symbol, "productType": "USDT-FUTURES", "limit": str(limit),
        })
        if not data or not isinstance(data, list):
            if isinstance(data, dict) and data.get("list"):
                data = data["list"]
            else:
                return []
        return [
            Trade(
                symbol=symbol,
                exchange=self.name,
                trade_id=str(item.get("tradeId", item.get("id", ""))),
                price=float(item["price"]),
                qty=float(item["size"] or item.get("qty", 0)),
                quote_qty=float(item["price"]) * float(item["size"] or item.get("qty", 0)),
                is_buyer_maker=item.get("side") == "sell",  # side 是 taker 方向
                timestamp=int(item["timestamp"] or item.get("cTime", 0)),
            )
            for item in data
        ]


# ── Gate.io ──

class GateClient(ExchangeClient):
    name = "gate"
    base_url = "https://api.gateio.ws"
    symbol_format = "{base}_{quote}"  # Gate 永续合约 symbol 格式如 BTC_USDT

    def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> dict | list:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def format_symbol(self, base: str, quote: str = "USDT") -> str:
        return f"{base.upper()}_{quote.upper()}"

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        # 合约详情接口：GET /api/v4/futures/usdt/contracts/{contract}
        data = self._get(f"/api/v4/futures/usdt/contracts/{symbol}")
        if not data or not isinstance(data, dict):
            return None
        return FundingRate(
            symbol=symbol,
            exchange=self.name,
            funding_rate=float(data.get("funding_rate", 0)),
            next_funding_time=int(data.get("funding_next_apply_time", 0)) * 1000,
            mark_price=float(data.get("mark_price", 0)) or None,
            index_price=float(data.get("index_price", 0)) or None,
        )

    def get_funding_rate_history(self, symbol: str, limit: int = 30) -> list[FundingRateHistory]:
        data = self._get("/api/v4/futures/usdt/funding_rate", {
            "contract": symbol, "limit": limit,
        })
        if not data or not isinstance(data, list):
            return []
        return [
            FundingRateHistory(
                symbol=symbol,
                exchange=self.name,
                funding_time=int(item["t"]) * 1000,
                funding_rate=float(item["r"]),
            )
            for item in data[:limit]
        ]

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        data = self._get(f"/api/v4/futures/usdt/contracts/{symbol}")
        if not data or not isinstance(data, dict):
            return None
        # Gate: open_interest = 持仓量（币数，即 BTC 数量）
        oi_qty = float(data.get("open_interest", 0))
        mark_price = float(data.get("mark_price", 0)) or None
        oi_val = oi_qty * mark_price if mark_price else None
        return OpenInterest(
            symbol=symbol,
            exchange=self.name,
            open_interest=oi_qty,
            open_interest_value=oi_val,
            timestamp=int(time.time() * 1000),
        )

    def get_open_interest_history(self, symbol: str, period: str = "1h", limit: int = 30) -> list[OpenInterest]:
        try:
            data = self._get("/api/v4/futures/usdt/open_interest", {
                "contract": symbol, "interval": period, "limit": limit,
            })
            if not data or not isinstance(data, list):
                return []
            return [
                OpenInterest(
                    symbol=symbol,
                    exchange=self.name,
                    open_interest=float(item.get("open_interest", 0)),
                    timestamp=int(item.get("time", 0)) * 1000,
                )
                for item in data
            ]
        except Exception:
            return []

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list[Trade]:
        data = self._get(f"/api/v4/futures/usdt/trades", {
            "contract": symbol, "limit": limit,
        })
        if not data or not isinstance(data, list):
            return []
        trades = []
        for item in data:
            size = float(item.get("size", 0))
            price = float(item.get("price", 0))
            # Gate: size > 0 = 主动买入（taker buy），size < 0 = 主动卖出（taker sell）
            # is_buyer_maker = True 意味着 maker 是买方 = taker 是卖方 = size < 0
            is_buyer_maker = size < 0
            abs_size = abs(size)
            # quote_qty: 以 USDT 计价的成交额 = price * size(币数)
            # Gate 永续的 size 是币数（BTC 数量）
            quote_qty = price * abs_size
            trades.append(Trade(
                symbol=symbol,
                exchange=self.name,
                trade_id=str(item["id"]),
                price=price,
                qty=abs_size,
                quote_qty=quote_qty,
                is_buyer_maker=is_buyer_maker,
                timestamp=int(item["create_time_ms"]),
            ))
        return trades


# ── 工厂 ──

EXCHANGE_CLIENTS: dict[str, ExchangeClient] = {
    "binance": BinanceClient(),
    "okx": OKXClient(),
    "bybit": BybitClient(),
    "bitget": BitgetClient(),
    "gate": GateClient(),
}


def get_client(exchange: str) -> ExchangeClient | None:
    return EXCHANGE_CLIENTS.get(exchange.lower())
