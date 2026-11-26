"""L4 执行层：paper 模式只记单不下单；live 模式预留 ccxt，默认禁用。

安全铁律：
  - 默认 paper，绝不因配置缺失静默转 live
  - live 必须 CT_MODE=live 且凭证齐全（Config.validate 已校验）
  - clientOrderId 幂等键保证重试不重复下单
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from config import Config
from decision import Decision


@dataclass
class ExecResult:
    ok: bool
    client_order_id: str
    status: str
    filled_qty: float = 0.0
    avg_price: float | None = None
    exchange_order_id: str = ""
    reason: str = ""


class BaseExecutor:
    def __init__(self, cfg: Config, store: Any, logger: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.log = logger or (lambda m: None)

    @staticmethod
    def client_order_id(dec: Decision) -> str:
        raw = f"ct_{dec.signal.dedup_key}_{dec.signal.action}_{int(time.time()*1000)}"
        return "ct_" + hashlib.sha1(raw.encode()).hexdigest()[:20]

    def execute(self, dec: Decision) -> ExecResult:
        raise NotImplementedError


class PaperExecutor(BaseExecutor):
    """纸面执行：只写 exec_order 表，不下真实单。默认且安全。"""

    def execute(self, dec: Decision) -> ExecResult:
        cid = self.client_order_id(dec)
        self.store.insert_exec_order({
            "client_order_id": cid,
            "signal_id": dec.signal_id,
            "exchange_order_id": "",
            "status": "paper_filled",
            "filled_qty": dec.qty,
            "avg_price": dec.signal.price,
        })
        self.log(f"[PAPER] {dec.signal.symbol} {dec.signal.action} qty={dec.qty:.6f} "
                 f"@ {dec.signal.price} lev={dec.leverage}x | {dec.reason}")
        return ExecResult(True, cid, "paper_filled", dec.qty, dec.signal.price,
                          reason="paper 模式：仅记录，未下真实单")


def build_order_params(dec: Decision, client_order_id: str) -> dict[str, Any]:
    """纯函数：构造币安 U 本位下单参数（可离线单测，不含网络/凭证）。

    修三个真实下单才会爆的坑：
      - 平/减仓必须 reduceOnly=True，否则可能反向开仓（币安双向持仓下尤其危险）
      - 单向持仓 BOTH 模式不能传 positionSide，传了会 400
      - 杠杆需显式设置（跟随交易员降级后的值，最多 3x）
    """
    params: dict[str, Any] = {"newClientOrderId": client_order_id}
    pos_side = (dec.signal.position_side or "BOTH").upper()
    is_closing = dec.signal.action.startswith(("CLOSE", "REDUCE"))
    if is_closing:
        params["reduceOnly"] = True
    if pos_side in ("LONG", "SHORT"):
        params["positionSide"] = pos_side
    # BOTH（单向）不传 positionSide
    return params


class LiveExecutor(BaseExecutor):
    """真实执行。仅在 CT_MODE=live 且凭证齐全时由工厂返回。

    安全：默认 paper，本类不会被实例化；ccxt 为可选依赖。
    实盘启用前须由用户自行确认：①凭证 ②策略期望收益 ③资金规模。AI 不代开。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import ccxt  # type: ignore
        self.exchange = ccxt.binance({
            "apiKey": self.cfg.api_key,
            "secret": self.cfg.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        self._leverage_set: set[str] = set()

    def _set_leverage(self, symbol: str, leverage: int) -> None:
        """每个品种只设一次杠杆（降级后的值，≤ CT_MAX_LEVERAGE）。"""
        if symbol in self._leverage_set:
            return
        try:
            self.exchange.set_leverage(int(leverage), symbol)
            self._leverage_set.add(symbol)
            self.log(f"[LIVE] 设置 {symbol} 杠杆 {leverage}x")
        except Exception as e:  # noqa: BLE001
            # 已设置过/接口不支持时继续（以交易所现有杠杆为准），不阻断下单
            self.log(f"[LIVE] 设置杠杆失败 {symbol}: {type(e).__name__}: {str(e)[:80]}")

    def execute(self, dec: Decision) -> ExecResult:
        cid = self.client_order_id(dec)
        try:
            symbol = dec.signal.symbol
            # 下单方向：signal.direction 是【成交方向】(+1买/-1卖)，
            # 平多=SELL(-1)、平空=BUY(+1)，直接用即可。
            side = "buy" if dec.signal.direction > 0 else "sell"
            params = build_order_params(dec, cid)
            self._set_leverage(symbol, int(dec.leverage or 1))
            # 数量精度：交给交易所按 stepSize 规整，避免精度错误拒单
            try:
                qty = self.exchange.amount_to_precision(symbol, dec.qty)
            except Exception:  # noqa: BLE001
                qty = dec.qty
            order = self.exchange.create_market_order(symbol, side, qty, params=params)
            self.store.insert_exec_order({
                "client_order_id": cid, "signal_id": dec.signal_id,
                "exchange_order_id": str(order.get("id", "")), "status": "live_submitted",
                "filled_qty": float(qty), "avg_price": dec.signal.price,
            })
            self.log(f"[LIVE] 提交 {symbol} {side} qty={qty} params={params}")
            return ExecResult(True, cid, "live_submitted", float(qty), dec.signal.price,
                              str(order.get("id", "")), "live 已提交")
        except Exception as e:  # noqa: BLE001
            self.store.insert_exec_order({
                "client_order_id": cid, "status": "failed", "filled_qty": 0,
            })
            return ExecResult(False, cid, "failed", reason=f"{type(e).__name__}: {str(e)[:200]}")


def build_executor(cfg: Config, store: Any, logger=None) -> BaseExecutor:
    """工厂：只有显式 live 模式才返回 LiveExecutor。"""
    if cfg.is_live:
        return LiveExecutor(cfg, store, logger)
    return PaperExecutor(cfg, store, logger)
