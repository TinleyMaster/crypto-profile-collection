"""Binance USD-M 永续合约签名客户端（子账户自动开单）。

职责：
- 签名请求（HMAC-SHA256）+ 服务器时间偏移校准（避免本地时钟漂移）
- 子账户 API Key 直接可用（子账户开启合约交易权限即可）
- 下单前自动查 exchangeInfo 的 LOT_SIZE / PRICE_FILTER，做数量与价格精度规整
- 支持一键设置杠杆、仓位查询、挂单/市价单、止损止盈（STOP_MARKET / TAKE_PROFIT_MARKET）

安全约定（重要）：
- 只做下单/撤单/查询，绝不提供提现相关能力
- 子账户 Key 应只开「合约交易」权限，不开提现权限
"""
from __future__ import annotations

import decimal
import hashlib
import hmac
import time
from typing import Any

import requests

FAPI_BASE = "https://fapi.binance.com"
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
        base_url: str = FAPI_BASE,
        timeout: int = TIMEOUT,
        recv_window: int = RECV_WINDOW,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
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
        """用服务器时间校准本地时钟偏移，只在首次需要时执行一次。"""
        if self._time_offset_ms is not None:
            return
        ts = self._public_request("/fapi/v1/time")["serverTime"]
        self._time_offset_ms = int(ts) - int(time.time() * 1000)

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> dict:
        """发请求。

        参数位置遵循 Binance 约定：
        - GET：参数进 URL 查询串
        - POST / DELETE：参数进请求体（application/x-www-form-urlencoded），
          签名同样基于该 body 串
        """
        params = dict(params or {})
        headers = dict(self.session.headers)
        body: str | None = None

        if signed:
            self._sync_time()
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = self.recv_window
            qs = self._query_string(params)
            qs += f"&signature={self._sign(qs)}"
            if method == "GET":
                url = f"{self.base_url}{path}?{qs}"
            else:
                url = f"{self.base_url}{path}"
                body = qs
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            url = f"{self.base_url}{path}"
            if params:
                url += f"?{self._query_string(params)}"

        req = requests.Request(method, url, headers=headers, data=body)
        prepared = self.session.prepare_request(req)
        try:
            resp = self.session.send(prepared, timeout=self.timeout)
        except requests.RequestException as e:
            # 网络层错误（连接重置/超时/断连等）统一包装，调用方可优雅降级
            raise BinanceFuturesError(f"[network] {path} {e}") from e
        if resp.status_code >= 400:
            raise BinanceFuturesError(
                f"[{resp.status_code}] {path} {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise BinanceFuturesError(f"[parse] {path} 非JSON响应: {resp.text[:200]}") from e
        if isinstance(data, dict) and data.get("code") is not None and data["code"] != 200:
            raise BinanceFuturesError(f"[{data.get('code')}] {data.get('msg')}")
        return data

    def _public_request(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params, signed=False)

    # ───────────────────────── 行情与账户 ─────────────────────────
    def ping(self) -> bool:
        try:
            self._public_request("/fapi/v1/ping")
            return True
        except Exception:
            return False

    def get_price(self, symbol: str) -> float:
        data = self._public_request("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_exchange_info(self, symbol: str) -> dict:
        """返回 symbol 的过滤规则（缓存）。"""
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

    def get_account(self) -> dict:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_balance(self, asset: str = "USDT") -> float:
        """子账户可用余额（USDT）。"""
        account = self.get_account()
        for b in account.get("assets", []):
            if b["asset"] == asset:
                return float(b.get("walletBalance", 0))
        return 0.0

    def get_position_risk(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)

    def get_position_amt(self, symbol: str) -> float:
        """当前持仓数量（含符号，负数为空单）。"""
        for p in self.get_position_risk(symbol):
            if p.get("symbol") == symbol:
                return float(p.get("positionAmt", 0))
        return 0.0

    def get_position_mode(self) -> str:
        """双开模式检测：dual=双向持仓，oneway=单向持仓。"""
        if self._position_mode is None:
            data = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
            self._position_mode = "dual" if data.get("dualSidePosition") else "oneway"
        return self._position_mode

    # ───────────────────────── 交易操作 ─────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": int(leverage)}, signed=True)

    def place_order(
        self,
        symbol: str,
        side: str,  # BUY / SELL
        order_type: str,  # LIMIT / MARKET
        quantity: float | None = None,
        price: float | None = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        close_position: bool = False,
        stop_price: float | None = None,
        position_side: str | None = None,
        new_client_order_id: str | None = None,
    ) -> dict:
        """下单（自动规整数量/价格精度）。

        Args:
            symbol: 合约代码，如 BTCUSDT
            side: BUY / SELL（开多 BUY、开空 SELL；平仓单用相反方向）
            order_type: LIMIT(挂单) / MARKET(市价) /
                        STOP_MARKET / TAKE_PROFIT_MARKET（配合 stop_price）
            quantity: 数量（必填，除非 close_position=True）
            price: 限价单价格
            time_in_force: GTC / IOC / FOK / POST_ONLY
            reduce_only: 只减仓
            close_position: 全部平仓（用于止损止盈单）
            stop_price: 触发价（STOP_MARKET / TAKE_PROFIT_MARKET 必填）
            position_side: 双向持仓模式下的持仓方向（LONG/SHORT）。单向模式传 None。
                          平仓单方向与开仓相反：平多=SELL+LONG，平空=BUY+SHORT。
        """
        info = self.get_exchange_info(symbol)
        params: dict[str, Any] = {"symbol": symbol, "side": side, "type": order_type}

        # 双向持仓模式必须显式指定 positionSide（单向模式不可传）
        if self.get_position_mode() == "dual":
            if not position_side:
                raise BinanceFuturesError(
                    "双向持仓模式下必须显式指定 position_side（LONG/SHORT）"
                )
            params["positionSide"] = position_side

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

        if reduce_only:
            params["reduceOnly"] = "true"
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id

        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: int | str) -> dict:
        return self._request("DELETE", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": order_id}, signed=True)

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)

    # ───────────────────────── 组合下单辅助 ─────────────────────────
    def open_position(
        self,
        symbol: str,
        direction: str,  # long / short
        notional_usdt: float,
        entry_price: float | None = None,
        leverage: int = 5,
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
    ) -> dict:
        """开仓（做多/做空）。

        - direction=long  -> BUY（position_side=LONG）
        - direction=short -> SELL（position_side=SHORT）
        - entry_price 为空时用市价单，否则用限价单挂在进场价
        - 自动设置杠杆；可选附带固定止损/止盈（STOP_MARKET / TAKE_PROFIT_MARKET 全平）
        """
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
            # 市价单：先查最新价估算数量（无法用 notional 参数，只能按现价换算）
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

        # 可选固定止损/止盈
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
        """挂止损/止盈平仓单（closePosition 全平，方向与开仓相反）。

        价格方向（相对进场价）：
        - 空单止损：进场价上方 pct%，平空 → BUY + SHORT
        - 空单止盈：进场价下方 pct%，平空 → BUY + SHORT
        - 多单止损：进场价下方 pct%，平多 → SELL + LONG
        - 多单止盈：进场价上方 pct%，平多 → SELL + LONG
        """
        info = self.get_exchange_info(symbol)
        # 平仓 side 与开仓相反
        close_side = "BUY" if direction == "short" else "SELL"
        position_side = "SHORT" if direction == "short" else "LONG"
        # 止损在亏损方向（反向），止盈在盈利方向（同向于价格对你不利/有利）
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
