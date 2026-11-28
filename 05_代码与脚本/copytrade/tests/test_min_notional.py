"""MIN_NOTIONAL 双层检查回归测试。

真实事故：get_exchange_info() 取回了 min_notional，但 decision/executor/daemon
全程没用 → 闸门截尾后剩几 USDT 的名义，币安 -1013 拒单。
本测试锁死：①决策层保守下限预检 ②执行层真实门槛+取整后终检 ③平仓能退场优先。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config, RiskGates  # noqa: E402
from decision import Decision, DecisionEngine  # noqa: E402
from executor import (  # noqa: E402
    LiveExecutor, PaperExecutor, build_executor,
    check_min_notional, effective_min_notional,
)
from state_machine import Confidence, FollowSignal, SignalStatus  # noqa: E402
from store import Store  # noqa: E402


def _sig(action="OPEN_LONG", price=100, qty=1, symbol="BTCUSDT"):
    return FollowSignal(
        symbol=symbol, position_side="LONG", action=action, qty=qty, direction=1,
        price=price, order_time=1700000000000, confidence=Confidence.HIGH,
        status=SignalStatus.READY, reason="", rule="test", dedup_key="k1",
    )


def _cfg(capital=1000, floor=5.0, **kw):
    return Config(allocated_capital=capital, mode="paper",
                  risk=RiskGates(min_notional_floor=floor, **kw))


# ── 纯函数 ──────────────────────────────────────────────────────

def test_check_min_notional_pass_above_threshold():
    ok, notional, why = check_min_notional(qty=0.1, price=100, min_notional=5)
    assert ok and abs(notional - 10) < 1e-9 and why == ""


def test_check_min_notional_reject_below_threshold():
    ok, notional, why = check_min_notional(qty=0.02, price=100, min_notional=5)
    assert not ok and abs(notional - 2) < 1e-9 and "MIN_NOTIONAL" in why


def test_check_min_notional_uses_rounded_qty():
    """关键：stepSize 向下取整后名义缩水，必须按取整后的 qty 判断。

    qty=0.104, step=0.01 → 取整 0.10 → 名义 10（达标）；
    qty=0.109, step=0.01 → 取整 0.10 → 名义 10（仍达标）。
    改成 qty=0.0509, step=0.01 → 0.05 → 名义 5；price=99 → 4.95 < 5 → 拒。
    """
    ok, notional, why = check_min_notional(
        qty=0.0509, price=99, min_notional=5, step_size=0.01)
    assert not ok, "取整后 0.05*99=4.95 < 5，必须拒单"
    assert abs(notional - 4.95) < 1e-6
    assert "0.0509" in why and "0.05" in why, "原因要暴露取整前后的 qty"


def test_check_min_notional_zero_floor_is_noop():
    ok, _, _ = check_min_notional(qty=0.001, price=1, min_notional=0)
    assert ok, "floor=0 时纯函数不做下限校验（交易所门槛另由 effective 取）"


def test_effective_min_notional_takes_max():
    class C:
        def get_exchange_info(self, symbol):
            return {"min_notional": 20.0, "step_size": 0.01}

    class NoInfo:
        pass

    assert effective_min_notional(C(), "BTCUSDT", floor=5.0) == 20.0
    assert effective_min_notional(C(), "BTCUSDT", floor=100.0) == 100.0
    assert effective_min_notional(NoInfo(), "BTCUSDT", floor=5.0) == 5.0, \
        "mock/取数失败时退回本地下限，不放行盲单"

    class Broken:
        def get_exchange_info(self, symbol):
            raise RuntimeError("network down")

    assert effective_min_notional(Broken(), "BTCUSDT", floor=7.0) == 7.0


# ── 决策层预检 ──────────────────────────────────────────────────

def test_decision_rejects_open_below_floor():
    # 映射后目标名义 = 1000 * (0.02*100/10000) = 0.2 < 5
    d = DecisionEngine(_cfg(floor=5.0), leader_aum=10000)
    r = d.decide(_sig(qty=0.02, price=100), 0, 0)
    assert not r.approved and "MIN_NOTIONAL" in r.reason


def test_decision_allows_close_below_floor():
    """平仓不受开仓闸限制（能退场优先），下限由执行层告警处理。"""
    d = DecisionEngine(_cfg(floor=5.0), leader_aum=10000)
    r = d.decide(_sig(action="CLOSE_LONG", qty=0.02, price=100), 0, 0)
    assert r.approved and "直通" in r.reason


def test_decision_floor_disabled():
    d = DecisionEngine(_cfg(floor=0.0), leader_aum=10000)
    r = d.decide(_sig(qty=0.02, price=100), 0, 0)
    assert r.approved, "floor=0 关闭本地下限"


def test_decision_floor_zero_cap_beats_positive():
    """闸门截尾到低于 floor 的部分必须被拦（这正是本次 bug 的触发路径）。"""
    # 单笔上限 0.001 → cap=1 USDT < floor=5
    d = DecisionEngine(_cfg(floor=5.0, max_single_notional_ratio=0.001),
                       leader_aum=10000)
    r = d.decide(_sig(qty=1, price=100), 0, 0)  # 原始目标 10 → 截尾 1
    assert not r.approved and "MIN_NOTIONAL" in r.reason
    assert "截尾" in r.reason, "原因要点明是截尾导致"


# ── 执行层终检 ──────────────────────────────────────────────────

def _live_exec(exchange_min=20.0, step_size=0.01):
    calls = []

    class MockClient:
        def set_leverage(self, symbol, lev):
            calls.append(("lev", symbol, lev))

        def get_exchange_info(self, symbol):
            return {"min_notional": exchange_min, "step_size": step_size}

        def place_order(self, **kw):
            calls.append(("order", kw))
            return {"orderId": 123}

        def get_order(self, symbol, oid):
            return {"avgPrice": "100", "executedQty": "0.1"}

    cfg = Config(mode="live", risk=RiskGates(min_notional_floor=5.0))
    st = Store(base_dir=Path(tempfile.mkdtemp()))
    ex = LiveExecutor(cfg, st, account="sub1", client=MockClient())
    return ex, st, calls


def test_live_rejects_open_below_exchange_threshold():
    ex, st, calls = _live_exec(exchange_min=20.0)
    # qty=0.1 @ 100 = 10 USDT < 交易所 20
    res = ex.execute(Decision(True, _sig(qty=0.1, price=100), qty=0.1, leverage=1))
    assert not res.ok and res.status == "rejected_min_notional"
    assert "MIN_NOTIONAL" in res.reason
    assert not any(c[0] == "order" for c in calls), "不足门槛绝不能调用 place_order"
    assert any(c[0] == "lev" for c in calls), "已设置的杠杆无需回滚"


def test_live_allows_open_above_exchange_threshold():
    ex, st, calls = _live_exec(exchange_min=20.0)
    res = ex.execute(Decision(True, _sig(qty=0.5, price=100), qty=0.5, leverage=1))
    assert res.ok and res.status == "live_submitted"
    assert any(c[0] == "order" for c in calls)


def test_live_close_bypasses_threshold_with_warning():
    """平仓低于门槛仍放行（能退场优先），日志显式告警。"""
    logs = []
    ex, st, calls = _live_exec(exchange_min=20.0)
    ex.log = logs.append
    res = ex.execute(Decision(True, _sig(action="CLOSE_LONG", qty=0.1, price=100),
                              qty=0.1, leverage=1))
    assert res.ok, "平仓不能被 MIN_NOTIONAL 硬拒"
    assert any(c[0] == "order" for c in calls)
    assert any("能退场优先" in m for m in logs), "必须留下告警痕迹"


def test_live_threshold_from_floor_when_client_has_no_info():
    """旧 mock 无 get_exchange_info：退回本地下限，不崩、不盲放行。"""
    class OldMock:
        def set_leverage(self, symbol, lev):
            pass

        def place_order(self, **kw):
            return {"orderId": 1}

        def get_order(self, symbol, oid):
            return {"avgPrice": "100", "executedQty": "0.1"}

    cfg = Config(mode="live", risk=RiskGates(min_notional_floor=5.0))
    st = Store(base_dir=Path(tempfile.mkdtemp()))
    ex = LiveExecutor(cfg, st, account="sub1", client=OldMock())
    res = ex.execute(Decision(True, _sig(qty=0.1, price=100), qty=0.1, leverage=1))
    assert res.ok and res.status == "live_submitted"

    res2 = ex.execute(Decision(True, _sig(qty=0.01, price=100), qty=0.01, leverage=1))
    assert not res2.ok and res2.status == "rejected_min_notional"


# ── paper 同样受决策层下限约束 ──────────────────────────────────

def test_paper_rejects_below_floor_via_decision():
    st = Store(base_dir=Path(tempfile.mkdtemp()))
    ex = build_executor(_cfg(floor=5.0), st)
    assert isinstance(ex, PaperExecutor)
    d = DecisionEngine(_cfg(floor=5.0), leader_aum=10000).decide(
        _sig(qty=0.02, price=100), 0, 0)
    assert not d.approved
    # paper executor 本身不重复校验（决策层已拦），直接执行也不会落单
    res = ex.execute(d)
    assert res.ok  # paper 只记单，但决策层已拒 → 正常流程不会走到这里
