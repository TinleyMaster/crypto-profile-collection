"""L3 决策层：资金映射（B2）+ 风控闸门 + 降级杠杆（C3）。

原则：宁可拒单，不可越闸。所有拦截都返回结构化 Decision，写 follow_signal.reason。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import Config
from state_machine import Confidence, FollowSignal, SignalStatus


@dataclass
class Decision:
    approved: bool
    signal: FollowSignal
    qty: float = 0.0
    leverage: float = 1.0
    notional: float = 0.0
    reason: str = ""
    signal_id: int | None = None


class DecisionEngine:
    def __init__(self, cfg: Config, leader_aum: float | None = None,
                 allocated_capital: float | None = None):
        self.cfg = cfg
        self.leader_aum = leader_aum  # 来自 position-history/detail；None 时无法用 B2，降级拒单
        self.gates = cfg.risk
        # 多账户：每账户用各自分配资金做 B2 映射；None 时回退全局（单账户兼容）
        self.capital = allocated_capital if allocated_capital is not None else cfg.allocated_capital

    def decide(self, sig: FollowSignal, current_exposure: float, symbol_exposure: float,
               kill_switch: bool = False, reconciliation_bad: bool = False) -> Decision:
        # 0. 状态/熔断/对账闸门
        if sig.status is not SignalStatus.READY:
            return Decision(False, sig, reason=f"信号未放行: {sig.status.value} ({sig.reason})")
        if kill_switch:
            return Decision(False, sig, reason="手动熔断开启：拒绝新单（仅允许平仓）")
        if reconciliation_bad:
            return Decision(False, sig, reason="镜像仓位对账偏差超限：暂停新单")
        # 1. B2 比例映射：需要对方 AUM，缺失则拒单（不猜）
        #    关键：开仓 AND 减仓都要按同一比例映射，否则减仓用交易员原始 qty
        #    会造成巨量平仓单（我方仓位远小于对方）。
        if not self.leader_aum or self.leader_aum <= 0:
            return Decision(False, sig, reason="缺少交易员 AUM，B2 比例映射无法计算 → 拒单")
        leader_notional = sig.qty * sig.price
        weight = leader_notional / self.leader_aum
        target_notional = self.capital * weight
        mapped_qty = target_notional / sig.price if sig.price > 0 else 0.0

        if sig.action.startswith("CLOSE") or sig.action.startswith("REDUCE"):
            # 平/减仓不受开仓闸门限制，优先放行（风控第一原则是能退场）
            # 但数量必须按 B2 同比例映射，且不得超过我方镜像持仓（execution 层再 clamp）
            if mapped_qty <= 0:
                return Decision(False, sig, reason="减仓映射数量 ≤ 0（价格为 0？）")
            return Decision(True, sig, qty=mapped_qty, leverage=1.0, notional=target_notional,
                            reason=f"减仓/平仓直通（按 B2 映射 {sig.qty}→{mapped_qty:.6f}，不受开仓闸限制）")


        # 2. 单笔上限
        cap_single = self.capital * self.gates.max_single_notional_ratio
        if target_notional > cap_single:
            target_notional = cap_single
            reason_note = f"单笔超限截尾至 {cap_single:.2f}"
        else:
            reason_note = ""

        # 3. 总敞口上限
        cap_total = self.capital * self.gates.max_total_exposure_ratio
        if current_exposure + target_notional > cap_total:
            room = max(0.0, cap_total - current_exposure)
            if room <= 0:
                return Decision(False, sig, reason=f"总敞口已满 {current_exposure:.2f}/{cap_total:.2f}")
            target_notional = room
            reason_note += f"；总敞口截尾至 {room:.2f}"

        # 4. 单品种集中度
        cap_symbol = self.capital * self.gates.max_symbol_concentration
        if symbol_exposure + target_notional > cap_symbol:
            room = max(0.0, cap_symbol - symbol_exposure)
            if room <= 0:
                return Decision(False, sig, reason=f"{sig.symbol} 集中度已满 {symbol_exposure:.2f}/{cap_symbol:.2f}")
            target_notional = room
            reason_note += f"；集中度截尾至 {room:.2f}"

        if target_notional <= 0:
            return Decision(False, sig, reason="闸门截尾后目标名义 ≤ 0")

        # 5. C3 降级杠杆：min(他杠杆, 我方上限)。这里无他杠杆字段时取 1x 保守
        leader_lev = float(sig.direction and 1 or 1)  # 占位；真实值见 apply_leader_leverage
        lev = min(leader_lev, self.gates.max_leverage)

        qty = target_notional / sig.price if sig.price > 0 else 0
        if qty <= 0:
            return Decision(False, sig, reason="映射后数量 ≤ 0（价格为 0？）")

        return Decision(
            True, sig, qty=qty, leverage=lev, notional=target_notional,
            reason=(reason_note or "B2 比例映射通过") + f"；杠杆降级至 {lev}x",
        )

    @staticmethod
    def apply_leader_leverage(dec: Decision, leader_leverage: float, max_leverage: float) -> Decision:
        """C3：min(他杠杆, 我方上限)，把占位 1x 替换为真实降级结果。"""
        if not dec.approved:
            return dec
        dec.leverage = min(float(leader_leverage or 1), max_leverage, dec.leverage or max_leverage)
        dec.reason += f"；跟随杠杆降级 min({leader_leverage},{max_leverage})={dec.leverage}"
        return dec
