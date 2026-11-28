"""L4 执行层：paper 只记单不下单；live 复用系统已有的 BinanceFuturesClient。

安全铁律：
  - 默认 paper，绝不因配置缺失静默转 live
  - live 必须 CT_MODE=live 且凭证齐全（Config.validate 已校验）
  - clientOrderId 幂等键含账户名，双账户不冲突
  - 不挂额外止损止盈：跟单是镜像交易员仓位，交易员平仓我们才平，
    额外挂 SL/TP 会偏离镜像语义（与策略开仓 open_position 不同）

关键：不另造轮子——直接复用
  scripts/src/crypto_research/clients/binance_futures.py::BinanceFuturesClient
  （HMAC 签名 + 服务器时间校准 + LOT_SIZE/PRICE_FILTER 精度 + 双开模式检测）
"""
from __future__ import annotations

import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from config import AccountConfig, Config
from decision import Decision

# 复用系统已有开仓客户端（经多个实盘事故修复的成熟实现）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "src"))
from crypto_research.clients.binance_futures import (  # noqa: E402
    BinanceFuturesClient,
    _round_down_to_step,
    is_permanent_binance_error,
)


def effective_min_notional(client: Any, symbol: str, floor: float) -> float:
    """交易所真实 MIN_NOTIONAL 与本地保守下限取大者。

    client 无 get_exchange_info（离线 mock）或取数失败时退回 floor，绝不静默放过。
    floor <= 0 时仍以交易所门槛为准（不关闭真实门槛）。
    """
    exchange_min = 0.0
    getter = getattr(client, "get_exchange_info", None)
    if callable(getter):
        try:
            exchange_min = float(getter(symbol).get("min_notional", 0) or 0)
        except Exception:  # noqa: BLE001 取不到就退回本地下限，不因查询失败下盲单
            exchange_min = 0.0
    return max(exchange_min, floor, 0.0)


def check_min_notional(qty: float, price: float, min_notional: float,
                       step_size: float = 0.0) -> tuple[bool, float, str]:
    """纯函数：按【LOT_SIZE 向下取整后的实际数量】校验名义价值。

    关键：币安先按 stepSize 截数量、再校验 notional。若用未取整的 qty 校验，
    '刚好达标' 的单会被截小后被交易所拒（-1013）。故这里先取整再算。
    """
    real_qty = _round_down_to_step(qty, step_size) if step_size > 0 else qty
    notional = real_qty * price
    if min_notional > 0 and notional < min_notional:
        return False, notional, (f"取整后名义 {notional:.4f} USDT "
                                 f"(qty {qty:.6f}→{real_qty:.6f} @ {price}) "
                                 f"< MIN_NOTIONAL {min_notional:.4f} USDT")
    return True, notional, ""


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
    def __init__(self, cfg: Config, store: Any, logger: Callable[[str], None] | None = None,
                 account: str = "default"):
        self.cfg = cfg
        self.store = store
        self.log = logger or (lambda m: None)
        self.account = account or "default"

    def client_order_id(self, dec: Decision) -> str:
        """幂等键：含账户名，双账户同信号也不撞主键。"""
        raw = (f"ct_{self.account}_{dec.signal.dedup_key}_{dec.signal.action}_"
               f"{int(time.time()*1000)}")
        return "ct_" + hashlib.sha1(raw.encode()).hexdigest()[:20]

    def execute(self, dec: Decision) -> ExecResult:
        raise NotImplementedError


class PaperExecutor(BaseExecutor):
    """纸面执行：只写 exec_order 表，不下真实单。默认且安全。"""

    def execute(self, dec: Decision) -> ExecResult:
        cid = self.client_order_id(dec)
        self.store.insert_exec_order({
            "client_order_id": cid,
            "account": self.account,
            "signal_id": dec.signal_id,
            "exchange_order_id": "",
            "status": "paper_filled",
            "filled_qty": dec.qty,
            "avg_price": dec.signal.price,
        })
        self.log(f"[PAPER/{self.account}] {dec.signal.symbol} {dec.signal.action} "
                 f"qty={dec.qty:.6f} @ {dec.signal.price} lev={dec.leverage}x | {dec.reason}")
        return ExecResult(True, cid, "paper_filled", dec.qty, dec.signal.price,
                          reason="paper 模式：仅记录，未下真实单")


def build_order_params(dec: Decision, client_order_id: str) -> dict[str, Any]:
    """纯函数：构造币安 U 本位下单参数（离线可测，不含网络/凭证）。

    修三个真实下单才会爆的坑：
      - 平/减仓必须 reduceOnly=True，否则可能反向开仓
      - 单向持仓 BOTH 不传 positionSide（传了会 400）
      - 杠杆由 live 执行层显式设置（跟随降级后的值）
    """
    params: dict[str, Any] = {"newClientOrderId": client_order_id}
    pos_side = (dec.signal.position_side or "BOTH").upper()
    is_closing = dec.signal.action.startswith(("CLOSE", "REDUCE"))
    if is_closing:
        params["reduceOnly"] = True
    if pos_side in ("LONG", "SHORT"):
        params["positionSide"] = pos_side
    return params


class LiveExecutor(BaseExecutor):
    """真实执行：复用 BinanceFuturesClient（系统已有实现）。

    仅在 CT_MODE=live + 凭证齐全时由工厂返回；默认 paper 不会实例化。
    实盘启用前须由用户自行确认：①凭证 ②策略期望收益 ③资金规模。AI 不代开。
    """

    def __init__(self, cfg: Config, store: Any,
                 logger: Callable[[str], None] | None = None,
                 account: str = "default",
                 acct_cfg: AccountConfig | None = None,
                 client: BinanceFuturesClient | None = None):
        super().__init__(cfg, store, logger, account)
        if client is not None:
            # 依赖注入：离线单测用 mock client，不发网络
            self.client = client
        else:
            acct = acct_cfg or AccountConfig(
                name=account, api_key=cfg.api_key, api_secret=cfg.api_secret,
                allocated_capital=cfg.allocated_capital,
            )
            if not acct.api_key or not acct.api_secret:
                raise ValueError(f"live 账户 {acct.name} 缺凭证（不输出密钥）")
            # ✅ 复用系统已有开仓客户端，不另造轮子
            self.client = BinanceFuturesClient(acct.api_key, acct.api_secret)
        self._leverage_set: set[str] = set()

    def _set_leverage(self, symbol: str, leverage: int) -> None:
        """每品种只设一次杠杆（降级后的值，≤ CT_MAX_LEVERAGE）。"""
        if symbol in self._leverage_set:
            return
        try:
            self.client.set_leverage(symbol, int(leverage))
            self._leverage_set.add(symbol)
            self.log(f"[LIVE/{self.account}] 设置 {symbol} 杠杆 {leverage}x")
        except Exception as e:  # noqa: BLE001 已设置过/接口不支持不阻断
            self.log(f"[LIVE/{self.account}] 设置杠杆失败 {symbol}: {type(e).__name__}: {str(e)[:80]}")

    def execute(self, dec: Decision) -> ExecResult:
        cid = self.client_order_id(dec)
        try:
            symbol = dec.signal.symbol
            params = build_order_params(dec, cid)
            self._set_leverage(symbol, int(dec.leverage or 1))
            # direction 是成交方向（+1 BUY / -1 SELL）；平多=SELL、平空=BUY，直接用
            side = "BUY" if dec.signal.direction > 0 else "SELL"
            pos_side = params.get("positionSide")  # LONG/SHORT 或 None(BOTH)

            # ✅ MIN_NOTIONAL 终检：交易所真实门槛 vs 本地保守下限取大者。
            # 开仓/加仓：不足门槛 → 硬拒（避免必被 -1013 拒掉的无谓请求）。
            # 平仓/减仓：只告警放行——"能退场优先"，硬拒会留下"仓位永远平不掉"
            # 的更大隐患（交易员继续平仓时映射 qty 仍可能低于门槛，形成死循环）。
            # 注意币安 reduceOnly 单同样受 MIN_NOTIONAL 约束，小单可能被拒，
            # 该风险以告警显式暴露，不静默吞掉。
            try:
                info = (self.client.get_exchange_info(symbol)
                        if callable(getattr(self.client, "get_exchange_info", None)) else {})
            except Exception:  # noqa: BLE001
                info = {}
            step_size = float(info.get("step_size", 0) or 0)
            required = effective_min_notional(
                self.client, symbol, float(self.cfg.risk.min_notional_floor))
            ok, notional, why = check_min_notional(
                dec.qty, dec.signal.price, required, step_size)
            is_closing = dec.signal.action.startswith(("CLOSE", "REDUCE"))
            if not ok and not is_closing:
                self.log(f"[LIVE/{self.account}] ✗ 拒单 {symbol} {dec.signal.action}: {why}")
                self.store.insert_exec_order({
                    "client_order_id": cid, "account": self.account,
                    "signal_id": dec.signal_id, "status": "rejected_min_notional",
                    "filled_qty": 0, "avg_price": None,
                })
                return ExecResult(False, cid, "rejected_min_notional", reason=why)
            if not ok and is_closing:
                self.log(f"[LIVE/{self.account}] ⚠ 平/减仓名义过小仍放行（能退场优先）: "
                         f"{symbol} {dec.signal.action}: {why}")

            # ✅ 走系统已有的 place_order：自动 LOT_SIZE 精度规整 + 双开模式检测
            # 不挂 SL/TP：跟单镜像交易员仓位，交易员平仓我们才平
            order = self.client.place_order(
                symbol=symbol, side=side, order_type="MARKET",
                quantity=dec.qty, position_side=pos_side,
                reduce_only=bool(params.get("reduceOnly", False)),
                new_client_order_id=cid,
            )
            oid = str(order.get("orderId", ""))
            # 回查真实成交均价/量（市价单 ACK 里 avgPrice 常为 0）
            filled_qty, avg_price = dec.qty, dec.signal.price
            if oid:
                try:
                    fo = self.client.get_order(symbol, oid)
                    avg_price = float(fo.get("avgPrice") or 0) or avg_price
                    filled_qty = float(fo.get("executedQty") or 0) or filled_qty
                except Exception as e:  # noqa: BLE001 best-effort，不阻断开仓记录
                    self.log(f"[LIVE/{self.account}] {symbol} 回查成交失败: {type(e).__name__}")
            self.store.insert_exec_order({
                "client_order_id": cid, "account": self.account,
                "signal_id": dec.signal_id, "exchange_order_id": oid,
                "status": "live_submitted", "filled_qty": filled_qty,
                "avg_price": avg_price,
            })
            self.log(f"[LIVE/{self.account}] 提交 {symbol} {side} qty={filled_qty:.6f} "
                     f"@ {avg_price} reduceOnly={bool(params.get('reduceOnly'))}")
            return ExecResult(True, cid, "live_submitted", filled_qty, avg_price, oid,
                              "live 已提交")
        except Exception as e:  # noqa: BLE001
            self.store.insert_exec_order({
                "client_order_id": cid, "account": self.account,
                "status": "failed", "filled_qty": 0,
            })
            permanent = is_permanent_binance_error(e)
            return ExecResult(False, cid, "failed",
                              reason=f"{'永久性' if permanent else '临时性'}错误: "
                                     f"{type(e).__name__}: {str(e)[:200]}")


def build_executor(cfg: Config, store: Any, logger=None, account: str = "default") -> BaseExecutor:
    """工厂（单账户语义，兼容旧测试）：只有显式 live 模式才返回 LiveExecutor。"""
    if cfg.is_live:
        return LiveExecutor(cfg, store, logger, account)
    return PaperExecutor(cfg, store, logger, account)


def build_account_executors(cfg: Config, store: Any, logger=None) -> dict[str, BaseExecutor]:
    """多账户工厂：为每个子账户建独立执行器（各带自己的凭证与分配资金）。

    paper 模式下无凭证也会建 PaperExecutor（用账户名做 key），便于双账户纸面验证。
    """
    accounts = cfg.active_accounts
    if not accounts:
        # 无账户配置：回退单账户（paper 用 default，live 会因 validate 提前拒绝）
        return {"default": build_executor(cfg, store, logger, "default")}
    out: dict[str, BaseExecutor] = {}
    for a in accounts:
        if cfg.is_live:
            out[a.safe_name()] = LiveExecutor(cfg, store, logger, a.safe_name(), acct_cfg=a)
        else:
            out[a.safe_name()] = PaperExecutor(cfg, store, logger, a.safe_name())
    return out
