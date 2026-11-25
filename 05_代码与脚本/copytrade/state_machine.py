"""L2 核心：成交流水 → 仓位状态机 → FollowSignal。

修硬伤③（H2 规则不可靠）——**彻底弃用 totalPnl==0 猜开平**。
三级判据，优先级从高到低：
  P1 显式字段：closePosition / reduceOnly 直接判为平/减，置信度 high
  P2 双向持仓（positionSide ∈ LONG/SHORT）：币安标准语义
       LONG+BUY=加多 / LONG+SELL=减多 / SHORT+SELL=加空 / SHORT+BUY=减空，置信度 high
  P3 单向持仓（positionSide=BOTH/空）：用镜像净持仓方向推断
       净=0 → BUY 开多 / SELL 开空；净>0 → BUY 加多 / SELL 减多；净<0 反之，置信度 medium
  totalPnl 仅作旁证：与判据冲突时**降级**置信度，绝不单独定开平。
  翻转（平仓量 > 现有净持仓）→ low 置信度 → HOLD 挂起，等 position-history 对账，不执行。

FIFO 配对算已实现盈亏；signed net_qty 约定：多=正，空=负。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalStatus(str, Enum):
    READY = "ready"                  # 可执行
    HOLD = "hold"                    # 挂起，等对账
    RECONCILED = "reconciled"        # 对账后放行


@dataclass
class Lot:
    qty: float          # 带符号：多为正，空为负
    price: float
    ts: int


@dataclass
class Mirror:
    symbol: str
    position_side: str
    lots: deque[Lot] = field(default_factory=deque)
    realized_pnl: float = 0.0

    @property
    def net_qty(self) -> float:
        return sum(l.qty for l in self.lots)

    @property
    def avg_price(self) -> float | None:
        gross = sum(abs(l.qty) for l in self.lots)
        if gross == 0:
            return None
        return sum(l.qty * l.price for l in self.lots) / sum(l.qty for l in self.lots)

    @property
    def is_flat(self) -> bool:
        return abs(self.net_qty) < 1e-8


@dataclass
class Classification:
    signed_delta: float              # 净持仓变化量（+=多方向，-=空方向）
    confidence: Confidence
    rule: str                        # 命中的判据，便于审计
    reason: str = ""


@dataclass
class FollowSignal:
    symbol: str
    position_side: str
    action: str                      # OPEN_LONG/ADD_LONG/REDUCE_LONG/CLOSE_LONG/... 或 HOLD
    qty: float                       # 我方应成交数量（绝对值）
    direction: int                   # +1 多 / -1 空
    price: float
    order_time: int
    confidence: Confidence
    status: SignalStatus
    reason: str
    rule: str
    dedup_key: str


def _as_bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"true", "1", "yes"}:
        return True
    if s in {"false", "0", "no"}:
        return False
    return None


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def classify(row: dict[str, Any], net_qty: float) -> Classification:
    """纯函数：一条成交 + 当前净持仓 → 分类。不依赖 DB，可单测。"""
    side = str(row.get("side", "")).strip().upper()
    pos_side = str(row.get("positionSide", "")).strip().upper()
    qty = _as_float(row.get("executedQty")) or 0.0
    if qty < 0:
        qty = abs(qty)
    reduce_only = _as_bool(row.get("reduceOnly"))
    close_position = _as_bool(row.get("closePosition"))
    pnl = _as_float(row.get("totalPnl"))

    is_buy = side == "BUY"
    is_sell = side == "SELL"
    if not qty or not (is_buy or is_sell):
        return Classification(0, Confidence.LOW, "invalid", f"缺 side/executedQty: side={side} qty={qty}")

    # ---- P1 显式平仓标记 ----
    if close_position is True or reduce_only is True:
        # 明确是减仓：方向由 pos_side 或净持仓决定
        if pos_side == "SHORT":
            signed = +qty       # 平空 → 净持仓向多方向回补
            rule = "p1_reduce_short"
        elif pos_side == "LONG" or net_qty > 0:
            signed = -qty       # 平多
            rule = "p1_reduce_long"
        elif net_qty < 0:
            signed = +qty
            rule = "p1_reduce_short"
        elif is_buy:
            signed = +qty
            rule = "p1_reduce_buy"
        else:
            signed = -qty
            rule = "p1_reduce_sell"
        # 翻转检测
        if abs(signed) > abs(net_qty) + 1e-8 and not (net_qty == 0 and (close_position or reduce_only)):
            return Classification(signed, Confidence.LOW, "p1_flip_guard",
                                  f"平仓量 {qty} > 净持仓 {net_qty}，疑似翻转，挂起")
        return Classification(signed, Confidence.HIGH, rule, "显式 reduceOnly/closePosition")

    # ---- P2 双向持仓标准语义 ----
    if pos_side in {"LONG", "SHORT"}:
        if pos_side == "LONG":
            signed = +qty if is_buy else -qty
            rule = "p2_long_buy" if is_buy else "p2_long_sell"
        else:  # SHORT
            signed = -qty if is_sell else +qty
            rule = "p2_short_sell" if is_sell else "p2_short_buy"
        # 减仓方向与净持仓相反且超量 → 翻转
        reducing = (signed * net_qty < 0)
        if reducing and abs(signed) > abs(net_qty) + 1e-8 and abs(net_qty) > 1e-8:
            return Classification(signed, Confidence.LOW, "p2_flip_guard",
                                  f"减仓量 {qty} > 净持仓 {net_qty}，挂起")
        conf = Confidence.HIGH
        reason = f"双向持仓 {pos_side}+{side}"
        # totalPnl 旁证：>0 通常意味着平仓已实现盈利，与加仓冲突则降级
        if pnl is not None and abs(pnl) > 1e-8 and not reducing:
            conf = Confidence.MEDIUM
            reason += "；totalPnl≠0 与加仓冲突，降级待对账"
        return Classification(signed, conf, rule, reason)

    # ---- P3 单向持仓：净持仓方向推断 ----
    if net_qty > 1e-8:      # 当前多
        signed = +qty if is_buy else -qty
        rule = "p3_long_add" if is_buy else "p3_long_reduce"
    elif net_qty < -1e-8:   # 当前空
        signed = -qty if is_sell else +qty
        rule = "p3_short_add" if is_sell else "p3_short_reduce"
    else:                   # 空仓
        signed = +qty if is_buy else -qty
        rule = "p3_flat_open_long" if is_buy else "p3_flat_open_short"

    reducing = (signed * net_qty < 0)
    if reducing and abs(signed) > abs(net_qty) + 1e-8:
        return Classification(signed, Confidence.LOW, "p3_flip_guard",
                              f"单向减仓量 {qty} > 净持仓 {net_qty}，疑似翻转，挂起")

    conf = Confidence.MEDIUM
    reason = f"单向持仓净={net_qty}，{side} 推断"
    if pnl is not None and abs(pnl) > 1e-8 and not reducing:
        conf = Confidence.LOW
        reason += "；totalPnl≠0 与开/加仓冲突，挂起待对账"
    # 空仓开仓：无历史方向可依，medium（不是 high）
    return Classification(signed, conf, rule, reason)


def action_name(old_net: float, signed: float, new_net: float) -> str:
    """由净持仓变化生成动作名。

    方向必须基于**被操作的仓位**（old_net），不是变化量 signed：
    全平多时 signed<0 但操作的是多仓，标 CLOSE_LONG 而非 CLOSE_SHORT。
    只有从空仓开仓时（old_net=0）才用 signed 定方向。
    """
    base = old_net if abs(old_net) > 1e-8 else signed
    direction = "LONG" if base > 0 else "SHORT"
    opening = abs(old_net) < 1e-8 or (old_net * signed > 0)
    closing = old_net * signed < 0
    if opening and abs(new_net) >= abs(old_net) - 1e-8:
        return ("OPEN_" if abs(old_net) < 1e-8 else "ADD_") + direction
    if closing:
        return ("CLOSE_" if abs(new_net) < 1e-8 else "REDUCE_") + direction
    return ("ADD_" if signed > 0 else "REDUCE_") + direction


class PositionStateMachine:
    """维护每个 (symbol, positionSide) 的镜像仓位，把成交流水转成跟随信号。"""

    def __init__(self, logger=None):
        self.mirrors: dict[tuple[str, str], Mirror] = {}
        self.log = logger or (lambda m: None)

    def get_mirror(self, symbol: str, side: str) -> Mirror:
        key = (symbol, side or "BOTH")
        if key not in self.mirrors:
            self.mirrors[key] = Mirror(symbol=symbol, position_side=key[1])
        return self.mirrors[key]

    def process(self, row: dict[str, Any], dedup_key: str) -> FollowSignal:
        symbol = str(row.get("symbol", ""))
        pos_side = str(row.get("positionSide", "")).strip().upper() or "BOTH"
        mirror = self.get_mirror(symbol, pos_side)
        old_net = mirror.net_qty

        cls = classify(row, old_net)
        price = _as_float(row.get("price")) or 0.0
        order_time = int(row.get("orderTime") or 0)
        qty_abs = abs(cls.signed_delta)

        if cls.confidence is Confidence.LOW or qty_abs == 0:
            return FollowSignal(
                symbol=symbol, position_side=pos_side, action="HOLD", qty=qty_abs,
                direction=(1 if cls.signed_delta > 0 else -1), price=price,
                order_time=order_time, confidence=cls.confidence, status=SignalStatus.HOLD,
                reason=cls.reason, rule=cls.rule, dedup_key=dedup_key,
            )

        # FIFO 应用：同向累加新 lot；反向按先进先出消耗
        new_net = self._apply_fifo(mirror, cls.signed_delta, price, order_time)
        act = action_name(old_net, cls.signed_delta, new_net)
        return FollowSignal(
            symbol=symbol, position_side=pos_side, action=act, qty=qty_abs,
            direction=(1 if cls.signed_delta > 0 else -1), price=price,
            order_time=order_time, confidence=cls.confidence, status=SignalStatus.READY,
            reason=cls.reason or "三级判据通过", rule=cls.rule, dedup_key=dedup_key,
        )

    @staticmethod
    def _apply_fifo(mirror: Mirror, signed: float, price: float, ts: int) -> float:
        remaining = signed
        # 反向先消耗
        while remaining and mirror.lots and (mirror.lots[0].qty * remaining < 0):
            lot = mirror.lots[0]
            if abs(lot.qty) <= abs(remaining):
                closed = mirror.lots.popleft()
                mirror.realized_pnl += (price - closed.price) * closed.qty
                remaining += closed.qty
            else:
                closed_qty = remaining
                mirror.realized_pnl += (price - lot.price) * closed_qty
                lot.qty += remaining
                remaining = 0
        if abs(remaining) > 1e-8:
            mirror.lots.append(Lot(qty=remaining, price=price, ts=ts))
        # 清理净≈0 的残 lot
        mirror.lots = deque(l for l in mirror.lots if abs(l.qty) > 1e-8)
        return mirror.net_qty

    def reconcile_from_position(self, positions: list[dict[str, Any]]) -> list[str]:
        """用 position-history 真实仓位校准镜像（A2 对账）。返回发现的偏差描述。"""
        diffs: list[str] = []
        if not positions:
            return diffs
        for p in positions:
            symbol = str(p.get("symbol", ""))
            side = str(p.get("positionSide", "")).strip().upper() or "BOTH"
            amt = _as_float(p.get("positionAmt") or p.get("qty") or p.get("amount")) or 0.0
            key = (symbol, side)
            m = self.get_mirror(symbol, side)
            gap = amt - m.net_qty
            if abs(gap) > 1e-6:
                diffs.append(f"{symbol}/{side}: 真实={amt} 镜像={m.net_qty:.6f} 偏差={gap:+.6f}")
                # 以真实仓位为准重建（单一 lot，价格用开仓均价）
                entry = _as_float(p.get("avgOpenPrice") or p.get("entryPrice") or p.get("price")) or (m.avg_price or 0.0)
                m.lots = deque([Lot(qty=amt, price=entry, ts=0)]) if abs(amt) > 1e-8 else deque()
        return diffs

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            f"{sym}|{side}": {
                "net_qty": round(m.net_qty, 8),
                "avg_price": round(m.avg_price, 8) if m.avg_price else None,
                "realized_pnl": round(m.realized_pnl, 8),
                "lots": len(m.lots),
            }
            for (sym, side), m in self.mirrors.items()
        }
