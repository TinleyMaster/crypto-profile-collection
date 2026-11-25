"""L3 决策闸门 + L4 paper 安全底线单测。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config, RiskGates  # noqa: E402
from decision import DecisionEngine  # noqa: E402
from executor import build_executor, PaperExecutor, LiveExecutor  # noqa: E402
from state_machine import FollowSignal, SignalStatus, Confidence  # noqa: E402
from store import Store  # noqa: E402


def sig(action="OPEN_LONG", price=100, qty=1, symbol="BTCUSDT", status=SignalStatus.READY):
    return FollowSignal(
        symbol=symbol, position_side="LONG", action=action, qty=qty, direction=1,
        price=price, order_time=1700000000000, confidence=Confidence.HIGH,
        status=status, reason="", rule="test", dedup_key="k1",
    )


def cfg(capital=1000, **kw):
    return Config(allocated_capital=capital, mode="paper", risk=RiskGates(**kw))


def test_missing_aum_rejects():
    d = DecisionEngine(cfg())
    r = d.decide(sig(), 0, 0)
    assert not r.approved and "AUM" in r.reason


def test_b2_ratio_mapping():
    d = DecisionEngine(cfg(), leader_aum=10000)  # 我方1000/他10000 = 0.1
    r = d.decide(sig(price=100, qty=1), 0, 0)     # 他名义100 → 我方目标10
    assert r.approved
    assert abs(r.notional - 10) < 1e-6
    assert abs(r.qty - 0.1) < 1e-6


def test_single_cap_truncates():
    d = DecisionEngine(cfg(1000, max_single_notional_ratio=0.05), leader_aum=10000)
    r = d.decide(sig(price=100, qty=10), 0, 0)  # 目标100 → 截尾50
    assert r.approved and abs(r.notional - 50) < 1e-6
    assert "截尾" in r.reason


def test_total_exposure_blocks():
    d = DecisionEngine(cfg(1000, max_total_exposure_ratio=0.6), leader_aum=10000)
    r = d.decide(sig(), current_exposure=600, symbol_exposure=0)
    assert not r.approved and "总敞口" in r.reason


def test_symbol_concentration_blocks():
    d = DecisionEngine(cfg(1000, max_symbol_concentration=0.3), leader_aum=10000)
    r = d.decide(sig(), current_exposure=0, symbol_exposure=300)
    assert not r.approved and "集中度" in r.reason


def test_close_bypasses_open_gates():
    # 有 AUM 时：平仓不受开仓闸限制，但数量按 B2 同比例映射
    d = DecisionEngine(cfg(), leader_aum=10000)
    r = d.decide(sig(action="CLOSE_LONG"), current_exposure=99999, symbol_exposure=99999)
    assert r.approved and "直通" in r.reason
    assert abs(r.qty - 0.1) < 1e-6, "减仓必须按比例映射，不能照搬交易员原始 qty"  # 名义10/价格100=0.1

    # 无 AUM：宁可拒单也不瞎平（缺比例依据）
    d2 = DecisionEngine(cfg(), leader_aum=None)
    assert not d2.decide(sig(action="CLOSE_LONG"), 0, 0).approved


def test_kill_switch_and_reconcile():
    d = DecisionEngine(cfg(), leader_aum=10000)
    assert not d.decide(sig(), 0, 0, kill_switch=True).approved
    assert not d.decide(sig(), 0, 0, reconciliation_bad=True).approved
    assert not d.decide(sig(status=SignalStatus.HOLD), 0, 0).approved


def test_paper_never_uses_live():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        st = Store(base_dir=Path(td))
        ex = build_executor(cfg(), st)
        assert isinstance(ex, PaperExecutor) and not isinstance(ex, LiveExecutor)
        d = DecisionEngine(cfg(), leader_aum=10000).decide(sig(), 0, 0)
        res = ex.execute(d)
        assert res.ok and res.status == "paper_filled"
        assert st.stats()["exec_order"] == 1


def test_live_requires_explicit_mode():
    try:
        Config(mode="live", api_key="", api_secret="").validate()
        assert False, "缺凭证的 live 模式必须抛错"
    except ValueError:
        pass
