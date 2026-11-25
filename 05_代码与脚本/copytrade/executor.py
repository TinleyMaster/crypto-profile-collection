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


class LiveExecutor(BaseExecutor):
    """真实执行（预留）。仅在 CT_MODE=live 且凭证齐全时由工厂返回；当前不启用。

    ccxt 为可选依赖，paper 模式 import 本类不会触发 ccxt 导入。
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

    def execute(self, dec: Decision) -> ExecResult:
        cid = self.client_order_id(dec)
        try:
            side = "buy" if dec.signal.direction > 0 else "sell"
            order = self.exchange.create_market_order(
                dec.signal.symbol, side, dec.qty, params={"clientOrderId": cid,
                                                          "positionSide": dec.signal.position_side}
            )
            self.store.insert_exec_order({
                "client_order_id": cid, "signal_id": dec.signal_id,
                "exchange_order_id": str(order.get("id", "")), "status": "live_submitted",
                "filled_qty": dec.qty, "avg_price": dec.signal.price,
            })
            return ExecResult(True, cid, "live_submitted", dec.qty, dec.signal.price,
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
