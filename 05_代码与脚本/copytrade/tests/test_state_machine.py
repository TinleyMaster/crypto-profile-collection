"""L2 状态机单测 — 覆盖方案 H2 未闭环的全部退化场景。

运行： cd 05_代码与脚本/copytrade && python -m pytest tests/ -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state_machine import (  # noqa: E402
    classify, action_name, PositionStateMachine, Confidence, SignalStatus,
)


def row(side, pos_side, qty, price, pnl=0, **kw):
    d = {"side": side, "positionSide": pos_side, "executedQty": qty,
         "price": price, "totalPnl": pnl, "orderTime": 1700000000000, "symbol": "BTCUSDT"}
    d.update(kw)
    return d


def test_p2_two_way_open_close_rules():
    """双向持仓：不依赖 totalPnl，靠 pos_side+side 判定。"""
    assert classify(row("SELL", "SHORT", 1, 100), net_qty=0).rule == "p2_short_sell"
    assert classify(row("SELL", "SHORT", 1, 100), net_qty=0).signed_delta == -1
    assert classify(row("BUY", "SHORT", 1, 100), net_qty=-1).signed_delta == +1
    assert classify(row("BUY", "LONG", 1, 100), net_qty=0).signed_delta == +1
    assert classify(row("SELL", "LONG", 1, 100), net_qty=1).signed_delta == -1


def test_p2_partial_close_zero_pnl_not_misclassified():
    """硬伤③核心用例：部分平仓 totalPnl=0 不再被误判为开仓。"""
    c = classify(row("SELL", "LONG", 0.5, 100, totalPnl=0), net_qty=1)
    assert c.signed_delta == -0.5, "平仓方向错了"
    assert c.rule == "p2_long_sell"
    assert c.confidence is Confidence.HIGH


def test_p1_reduce_only_flag_overrides_pnl():
    """P1 显式 reduceOnly 最高优先级，即使 totalPnl=0。"""
    c = classify(row("BUY", "BOTH", 1, 100, totalPnl=0, reduceOnly=True), net_qty=-1)
    assert c.rule == "p1_reduce_short"
    assert c.signed_delta == +1
    assert c.confidence is Confidence.HIGH


def test_p1_close_position_flag():
    c = classify(row("SELL", "LONG", 2, 100, closePosition=True), net_qty=2)
    assert c.signed_delta == -2
    assert c.confidence is Confidence.HIGH


def test_one_way_inference():
    """单向持仓：净持仓方向推断。"""
    assert classify(row("BUY", "BOTH", 1, 100), net_qty=0).rule == "p3_flat_open_long"
    assert classify(row("SELL", "BOTH", 1, 100), net_qty=0).rule == "p3_flat_open_short"
    assert classify(row("SELL", "BOTH", 0.5, 100), net_qty=1).signed_delta == -0.5
    assert classify(row("BUY", "BOTH", 0.5, 100), net_qty=-1).signed_delta == +0.5


def test_flip_guard_holds():
    """减仓量 > 净持仓 → 低置信度 HOLD（不让翻转乱执行）。"""
    c = classify(row("SELL", "LONG", 2, 100), net_qty=1)
    assert c.confidence is Confidence.LOW
    assert "flip" in c.rule


def test_pnl_conflict_downgrades():
    """加仓却带 totalPnl≠0 → 降级，不盲信。"""
    c = classify(row("BUY", "LONG", 1, 100, totalPnl=5), net_qty=1)
    assert c.confidence is Confidence.MEDIUM
    assert "totalPnl" in c.reason


def test_invalid_row_holds():
    c = classify({"side": "BUY", "executedQty": 0, "symbol": "BTCUSDT"}, net_qty=0)
    assert c.confidence is Confidence.LOW
    assert c.rule == "invalid"


def test_action_names():
    assert action_name(0, 1, 1) == "OPEN_LONG"
    assert action_name(1, 1, 2) == "ADD_LONG"
    assert action_name(1, -0.5, 0.5) == "REDUCE_LONG"
    assert action_name(1, -1, 0) == "CLOSE_LONG"
    assert action_name(0, -1, -1) == "OPEN_SHORT"
    assert action_name(-1, 1, 0) == "CLOSE_SHORT"


def test_fifo_sequence_open_add_partial_full_close():
    sm = PositionStateMachine()
    s1 = sm.process(row("BUY", "LONG", 1, 100), "k1")
    assert s1.action == "OPEN_LONG" and s1.status is SignalStatus.READY
    s2 = sm.process(row("BUY", "LONG", 1, 110), "k2")
    assert s2.action == "ADD_LONG"
    m = sm.get_mirror("BTCUSDT", "LONG")
    assert abs(m.net_qty - 2) < 1e-8
    s3 = sm.process(row("SELL", "LONG", 1, 120, totalPnl=0), "k3")
    assert s3.action == "REDUCE_LONG", "部分平仓 totalPnl=0 必须判为减仓"
    assert abs(m.net_qty - 1) < 1e-8
    # 已实现盈亏：(120-100)*1 = 20
    assert abs(m.realized_pnl - 20) < 1e-8
    s4 = sm.process(row("SELL", "LONG", 1, 130), "k4")
    assert s4.action == "CLOSE_LONG"
    assert m.is_flat


def test_reconcile_from_position():
    sm = PositionStateMachine()
    sm.process(row("BUY", "LONG", 1, 100), "k1")
    diffs = sm.reconcile_from_position([
        {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": 0.8, "avgOpenPrice": 105}
    ])
    assert len(diffs) == 1
    assert abs(sm.get_mirror("BTCUSDT", "LONG").net_qty - 0.8) < 1e-8


def test_legacy_h2_rule_would_fail():
    """对照：旧 totalPnl==0 规则在此场景会判成开仓——证明修复必要。"""
    r = row("SELL", "LONG", 0.5, 100, totalPnl=0)
    legacy_open = (r["totalPnl"] == 0 and r["side"] == "SELL" and r["positionSide"] == "LONG")
    assert legacy_open is True, "旧规则确实会把这次部分平仓当开空/开仓"
    fixed = classify(r, net_qty=1)
    assert fixed.signed_delta < 0, "新规则必须判为减仓"
