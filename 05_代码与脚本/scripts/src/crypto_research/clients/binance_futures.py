"""Binance Portfolio Margin（统一账户/U本位）签名客户端。

⚠️  你的账户是币安「统一账户/Portfolio Margin」，必须用 papi.binance.com，
   不能用 fapi.binance.com（纯 U 本位合约账户域名，PM 账户在 fapi 上永远 -2015）。

域名分工：
  - papi.binance.com：签名请求（余额、账户、持仓、下单、杠杆）
  - fapi.binance.com：公网行情（exchangeInfo、ticker/price——papi 上没有这两个端点）

职责：
- HMAC-SHA256 签名 + 服务器时间偏移校准
- 下单前自动查 fapi exchangeInfo 做数量/价格精度规整
- 支持一键设杠杆、挂单/市价单、止损止盈（STOP_MARKET / TAKE_PROFIT_MARKET）、跟踪止盈

安全约定：
- 只做下单/撤单/查询，绝不提供提现
- Key 应只开「合约交易」权限，不开提现
"""
from __future__ import annotations

import decimal
import hashlib
import hmac
import time
from typing import Any

import requests

PAPI_BASE = "https://papi.binance.com"   # 签名请求（PM 专属）
FAPI_BASE = "https://fapi.binance.com"   # 公网行情（exchangeInfo/ticker 只有 fapi 有）
TIMEOUT = 15
RECV_WINDOW = 5000


class BinanceFuturesError(Exception):
    """Binance API 错误（含业务码）。"""


def _round_down_to_step(value: float, step: float) -> float:
    """向下规整到 stepSize 的整数倍（数量必须向下，避免超限被拒）。"""
    d = decimal.Decimal(str(value))
    s = decimal.Decimal(str(step))
    if s <= 0:
        return float(d)
    return float((d / s).to_integral_value(rounding=decimal.ROUND_DOWN) * s)


def _round_price_to_tick(value: float, tick: float) -> float:
    """价格四舍五入到 tickSize 的整数倍。"""
    d = decimal.Decimal(str(value))
    t = decimal.Decimal(str(tick))
    if t <= 0:
        return float(d)
    return float((d / t).to_integral_value(rounding=decimal.ROUND_HALF_UP) * t)


class BinanceFuturesClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = PAPI_BASE,       # 签名端点（PM 默认 papi）
        market_base_url: str = FAPI_BASE, # 公网行情（papi 上没有 exchangeInfo/ticker）
        timeout: int = TIMEOUT,
        recv_window: int = RECV_WINDOW,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.market_base_url = market_base_url.rstrip("/")
        self.timeout = timeout
        self.recv_window = recv_window
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self._time_offset_ms: int | None = None
        self._exchange_info: dict[str, dict] = {}
        self._position_mode: str | None = None  # "dual" | "oneway"

    # ───────────────────────── 基础设施 ─────────────────────────
    def _query_string(self, params: dict) -> str:
        return "&".join(
            f"{k}={v}" for k, v in sorted(params.items()) if v is not None
        )

    def _sign(self, qs: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _sync_time(self) -> None:
        """用 papi 服务器时间校准本地时钟偏移。"""
        if self._time_offset_ms is not None:
            return
        ts = self._request("GET", "/papi/v1/time", base=self.base_url, signed=False)["serverTime"]
        self._time_offset_ms = int(ts) - int(time.time() * 1000)

    def _request(
        self, method: str, path: str,
        params: dict | None = None,
        signed: bool = False,
        base: str | None = None,
    ) -> dict:
        """发请求。base=None 时根据 signed 自动选（signed→papi，unsinged→market_base=fapi）。"""
        params = dict(params or {})
        headers = dict(self.session.headers)
        body: str | None = None

        if base is None:
            base = self.base_url if signed else self.market_base_url

        if signed:
            self._sync_time()
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = self.recv_window
            qs = self._query_string(params)
            qs += f"&signature={self._sign(qs)}"
            if method == "GET":
                url = f"{base}{path}?{qs}"
            else:
                url = f"{base}{path}"
                body = qs
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            url = f"{base}{path}"
            if params:
                url += f"?{self._query_string(params)}"

        req = requests.Request(method, url, headers=headers, data=body)
        prepared = self.session.prepare_request(req)
        try:
            resp = self.session.send(prepared, timeout=self.timeout)
        except requests.RequestException as e:
            raise BinanceFuturesError(f"[network] {path} {e}") from e
        if resp.status_code >= 400:
            raise BinanceFuturesError(
                f"[{resp.status_code}] {path} {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise BinanceFuturesError(f"[parse] {path} 非JSON响应: {resp.text[:200]}") from e
        # PM 和 fapi 都用 code != 200 表示业务错误
        if isinstance(data, dict) and data.get("code") is not None and data["code"] != 200:
            raise BinanceFuturesError(f"[{data.get('code')}] {data.get('msg')}")
        return data

    def _public_request(self, path: str, params: dict | None = None) -> dict:
        """公网请求（走 market_base = fapi）。"""
        return self._request("GET", path, params, signed=False, base=self.market_base_url)

    def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        """签名请求（走 base_url = papi）。"""
        return self._request(method, path, params, signed=True, base=self.base_url)

    # ───────────────────────── 行情（走 fapi 公网端点） ─────────────────────────
    def ping(self) -> bool:
        """papi ping（可走 papi 公网端点）。"""
        try:
            self._request("GET", "/papi/v1/ping", signed=False, base=self.base_url)
            return True
        except Exception:
            return False

    def get_price(self, symbol: str) -> float:
        data = self._public_request("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_exchange_info(self, symbol: str) -> dict:
        """返回 symbol 的过滤规则（缓存，走 fapi 的公网 exchangeInfo）。"""
        if symbol not in self._exchange_info:
            data = self._public_request("/fapi/v1/exchangeInfo", {"symbol": symbol})
            sym = data["symbols"][0]
            filters = {f["filterType"]: f for f in sym["filters"]}
            info = {
                "quantity_precision": sym.get("quantityPrecision", 8),
                "price_precision": sym.get("pricePrecision", 8),
                "min_qty": float(filters.get("LOT_SIZE", {}).get("minQty", 0) or 0),
                "step_size": float(filters.get("LOT_SIZE", {}).get("stepSize", 1) or 1),
                "tick_size": float(filters.get("PRICE_FILTER", {}).get("tickSize", 1) or 1),
                "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("notional", 0) or 0),
            }
            self._exchange_info[symbol] = info
        return self._exchange_info[symbol]

    # ───────────────────────── 账户（走 papi） ─────────────────────────
    def get_account(self) -> dict:
        """PM 账户概览（/papi/v1/account）。

        返回字段（PM 平铺格式，无 assets 数组）：
          accountEquity, totalWalletBalance, totalAvailableBalance,
          accountInitialMargin, accountMaintMargin, accountStatus, ...
        """
        return self._signed_request("GET", "/papi/v1/account")

    def get_balance(self, asset: str = "USDT") -> float:
        """子账户可用余额（PM /papi/v1/balance，返回数组）。"""
        data = self._signed_request("GET", "/papi/v1/balance")
        # PM balance 返回数组，每项一个 asset（或 account dict）
        if isinstance(data, list):
            for item in data:
                if item.get("asset") == asset:
                    return float(item.get("totalWalletBalance", 0) or 0)
        # 兜底：从 /papi/v1/account 取 totalWalletBalance
        acct = self.get_account()
        return float(acct.get("totalWalletBalance", 0) or 0)

    def get_position_risk(self, symbol: str | None = None) -> list[dict]:
        """PM U 本位持仓（/papi/v1/um/positionRisk）。"""
        params = {"symbol": symbol} if symbol else None
        return self._signed_request("GET", "/papi/v1/um/positionRisk", params=params)

    def get_position_amt(self, symbol: str) -> float:
        """当前持仓数量（含符号，负数为空单）。"""
        for p in self.get_position_risk(symbol):
            if p.get("symbol") == symbol:
                return float(p.get("positionAmt", 0))
        return 0.0

    def get_income(self, symbol: str | None = None,
                   start_ms: int | None = None, end_ms: int | None = None,
                   limit: int = 500) -> list[dict]:
        """已实现收益流水（PM /papi/v1/um/income）。"""
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if start_ms:
            params["startTime"] = start_ms
        if end_ms:
            params["endTime"] = end_ms
        return self._signed_request("GET", "/papi/v1/um/income", params=params)

    def get_position_mode(self) -> str:
        """双开模式检测：dual=双向持仓，oneway=单向持仓。"""
        if self._position_mode is None:
            data = self._signed_request("GET", "/papi/v1/positionSide/dual")
            self._position_mode = "dual" if data.get("dualSidePosition") else "oneway"
        return self._position_mode

    # ───────────────────────── 交易操作（走 papi/v1/um） ─────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._signed_request("POST", "/papi/v1/um/leverage",
                                    {"symbol": symbol, "leverage": int(leverage)})

    def place_order(
        self,
        symbol: str,
        side: str,  # BUY / SELL
        order_type: str,  # LIMIT / MARKET / STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET
        quantity: float | None = None,
        price: float | None = None,
        time_in_force: str = "GTC",   # PM 账户只支持 GTC / IOC / FOK（不支持 POST_ONLY）
        reduce_only: bool = False,
        close_position: bool = False,
        stop_price: float | None = None,
        position_side: str | None = None,
        new_client_order_id: str | None = None,
        callback_rate: float | None = None,
    ) -> dict:
        """下单（PM /papi/v1/um/order，自动规整精度）。

        Args:
            time_in_force: GTC / IOC / FOK（⚠️ PM 账户不支持 POST_ONLY）
            position_side: PM 统一账户用单向持仓模式居多，默认不传 positionSide
        """
        info = self.get_exchange_info(symbol)
        params: dict[str, Any] = {"symbol": symbol, "side": side, "type": order_type}

        # PM 统一账户默认单向持仓模式（oneway），不需要显式 positionSide
        # 如果检测到双向持仓（dual）才需要传
        try:
            if self.get_position_mode() == "dual":
                if not position_side:
                    raise BinanceFuturesError("双向持仓模式下必须显式指定 position_side（LONG/SHORT）")
                params["positionSide"] = position_side
        except BinanceFuturesError:
            # 检测失败时保守地不传（PM 默认 oneway）
            pass

        if close_position:
            params["closePosition"] = "true"
        elif quantity is not None:
            params["quantity"] = _round_down_to_step(float(quantity), info["step_size"])

        if order_type in ("LIMIT", "STOP", "TAKE_PROFIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if stop_price is not None:
                params["stopPrice"] = _round_price_to_tick(float(stop_price), info["tick_size"])
            if order_type == "LIMIT":
                params["price"] = _round_price_to_tick(float(price), info["tick_size"])
                params["timeInForce"] = time_in_force

        if order_type == "TRAILING_STOP_MARKET":
            if callback_rate is None:
                raise BinanceFuturesError("TRAILING_STOP_MARKET 必须传 callback_rate（%）")
            params["callbackRate"] = f"{callback_rate:.1f}"
            params["workingType"] = "CONTRACT_PRICE"

        if reduce_only:
            params["reduceOnly"] = "true"
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id

        return self._signed_request("POST", "/papi/v1/um/order", params)

    def cancel_order(self, symbol: str, order_id: int | str) -> dict:
        return self._signed_request("DELETE", "/papi/v1/um/order",
                                    {"symbol": symbol, "orderId": order_id})

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else None
        return self._signed_request("GET", "/papi/v1/um/openOrders", params=params)

    # ───────────────────────── 组合下单辅助 ─────────────────────────
    def place_trailing_stop(
        self,
        symbol: str,
        direction: str,  # long / short
        callback_rate: float,  # 回调比例 %，如 3.0 = 3%
        quantity: float | None = None,
        close_position: bool = True,
    ) -> dict:
        """挂跟踪止盈平仓单（TRAILING_STOP_MARKET）。"""
        close_side = "SELL" if direction == "long" else "BUY"
        position_side = "LONG" if direction == "long" else "SHORT"
        return self.place_order(
            symbol=symbol, side=close_side, order_type="TRAILING_STOP_MARKET",
            quantity=quantity if not close_position else None,
            close_position=close_position,
            position_side=position_side,
            callback_rate=callback_rate,
            new_client_order_id=f"trl{direction[:1]}{callback_rate:.0f}",
        )

    def open_position(
        self,
        symbol: str,
        direction: str,  # long / short
        notional_usdt: float,
        entry_price: float | None = None,
        leverage: int = 5,
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        trailing_stop_pct: float = 0.0,
    ) -> dict:
        """开仓（做多/做空）——PM 统一账户。"""
        side = "BUY" if direction == "long" else "SELL"
        position_side = "LONG" if direction == "long" else "SHORT"
        self.set_leverage(symbol, leverage)

        info = self.get_exchange_info(symbol)
        cur: float | None = None
        if entry_price is not None:
            price = _round_price_to_tick(entry_price, info["tick_size"])
            qty = _round_down_to_step(notional_usdt / price, info["step_size"])
            order_type, order_price = "LIMIT", price
        else:
            cur = self.get_price(symbol)
            qty = _round_down_to_step(notional_usdt / cur, info["step_size"])
            order_type, order_price = "MARKET", None

        if qty <= 0:
            raise BinanceFuturesError(
                f"计算下单数量为 0（notional={notional_usdt}, price={order_price or cur}）"
            )

        result = self.place_order(
            symbol=symbol, side=side, order_type=order_type,
            quantity=qty, price=order_price, position_side=position_side,
        )
        result["_qty"] = qty
        result["_price"] = order_price or cur
        entry_used = order_price or cur

        # 可选跟踪止盈 / 固定止损/止盈
        if trailing_stop_pct and trailing_stop_pct > 0:
            try:
                trl = self.place_trailing_stop(symbol, direction, trailing_stop_pct)
                result["_trailing_stop"] = trl
            except BinanceFuturesError as e:
                result["_trailing_stop"] = {"error": str(e)}
        else:
            if stop_loss_pct and stop_loss_pct > 0:
                try:
                    sl = self.place_sltp(symbol, direction, entry_used, stop_loss_pct, is_stop=True)
                    result["_stop_loss"] = sl
                except BinanceFuturesError as e:
                    result["_stop_loss"] = {"error": str(e)}
        if take_profit_pct and take_profit_pct > 0:
            try:
                tp = self.place_sltp(symbol, direction, entry_used, take_profit_pct, is_stop=False)
                result["_take_profit"] = tp
            except BinanceFuturesError as e:
                result["_take_profit"] = {"error": str(e)}

        return result

    def place_sltp(
        self,
        symbol: str,
        direction: str,  # long / short（持仓方向）
        entry_price: float,
        pct: float,
        is_stop: bool,  # True=止损(STOP_MARKET 全平) / False=止盈(TAKE_PROFIT_MARKET 全平)
    ) -> dict:
        """挂止损/止盈平仓单（closePosition 全平，方向与开仓相反）。"""
        info = self.get_exchange_info(symbol)
        close_side = "BUY" if direction == "short" else "SELL"
        position_side = "SHORT" if direction == "short" else "LONG"
        if is_stop:
            trigger_price = entry_price * (1 + pct / 100) if direction == "short" else entry_price * (1 - pct / 100)
            order_type = "STOP_MARKET"
        else:
            trigger_price = entry_price * (1 - pct / 100) if direction == "short" else entry_price * (1 + pct / 100)
            order_type = "TAKE_PROFIT_MARKET"

        trigger_price = _round_price_to_tick(trigger_price, info["tick_size"])
        tag = "sl" if is_stop else "tp"
        return self.place_order(
            symbol=symbol, side=close_side, order_type=order_type,
            stop_price=trigger_price, close_position=True,
            position_side=position_side,
            new_client_order_id=f"{tag}{direction[:1]}{int(trigger_price)}",
        )
