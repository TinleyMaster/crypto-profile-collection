"""锁住双账户跟单的三个坑：账户隔离 / 幂等键唯一 / 复用系统开仓客户端。"""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import AccountConfig, Config  # noqa: E402
from daemon import CopyTradeDaemon  # noqa: E402
from decision import Decision  # noqa: E402
from executor import PaperExecutor, build_account_executors  # noqa: E402
from state_machine import Confidence, FollowSignal, SignalStatus  # noqa: E402
from store import Store  # noqa: E402


def _sig(action="OPEN_LONG", direction=None):
    # direction = 成交方向（+1 BUY / -1 SELL）；平多=SELL，平空=BUY
    if direction is None:
        direction = -1 if action.startswith(("CLOSE", "REDUCE")) and action.endswith("LONG") else 1
    return FollowSignal(symbol="BTCUSDT", position_side="LONG", action=action,
                        qty=1, direction=direction, price=42000, order_time=1,
                        confidence=Confidence.HIGH, status=SignalStatus.READY,
                        reason="", rule="", dedup_key="k1")


def _cfg(accounts):
    return Config(mode="paper", accounts=tuple(accounts), replay_mode=True)


def test_two_accounts_parsed_from_env(monkeypatch):
    """CT_ACCOUNT_N_* 逐号解析，遇缺 key 停止。"""
    import os
    for k in list(os.environ):
        if k.startswith("CT_ACCOUNT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CT_ACCOUNT_1_API_KEY", "k1")
    monkeypatch.setenv("CT_ACCOUNT_1_API_SECRET", "s1")
    monkeypatch.setenv("CT_ACCOUNT_1_CAPITAL", "1500")
    monkeypatch.setenv("CT_ACCOUNT_2_API_KEY", "k2")
    monkeypatch.setenv("CT_ACCOUNT_2_API_SECRET", "s2")
    monkeypatch.setenv("CT_ACCOUNT_2_CAPITAL", "2500")
    monkeypatch.setenv("CT_ACCOUNT_3_API_KEY", "")  # 空 → 停止
    accts = Config._parse_accounts()
    assert len(accts) == 2
    assert accts[0].name == "sub1" and accts[0].allocated_capital == 1500.0
    assert accts[1].name == "sub2" and accts[1].allocated_capital == 2500.0


def test_account_executors_are_isolated():
    cfg = _cfg([AccountConfig("sub1", "k1", "s1", 1000),
                AccountConfig("sub2", "k2", "s2", 2000)])
    exs = build_account_executors(cfg, None)
    assert set(exs) == {"sub1", "sub2"}
    assert exs["sub1"].account == "sub1" and exs["sub2"].account == "sub2"
    assert all(isinstance(e, PaperExecutor) for e in exs.values())


def test_client_order_id_unique_across_accounts():
    """同一信号两账户的 client_order_id 必须不同（否则 exec_order 主键冲突）。"""
    cfg = _cfg([AccountConfig("sub1", "k1", "s1", 1000),
                AccountConfig("sub2", "k2", "s2", 2000)])
    exs = build_account_executors(cfg, None)
    d = Decision(True, _sig(), qty=0.1, leverage=1)
    cid1 = exs["sub1"].client_order_id(d)
    cid2 = exs["sub2"].client_order_id(d)
    assert cid1 != cid2, "双账户同信号幂等键撞车"


def test_exec_order_account_isolation():
    """store 按账户隔离查询，A 账户查不到 B 账户的单。"""
    st = Store(base_dir=Path(tempfile.mkdtemp()))
    st.insert_exec_order({"client_order_id": "a1", "account": "sub1",
                          "status": "paper_filled", "filled_qty": 1})
    st.insert_exec_order({"client_order_id": "b1", "account": "sub2",
                          "status": "paper_filled", "filled_qty": 2})
    assert len(st.iter_exec_orders("sub1")) == 1
    assert len(st.iter_exec_orders("sub2")) == 1
    assert len(st.iter_exec_orders()) == 2


def test_signal_dedup_key_account_prefixed():
    """双账户同成交信号的 dedup_key 必须带账户前缀，不撞 UNIQUE。"""
    st = Store(base_dir=Path(tempfile.mkdtemp()))
    assert st.insert_signal({"dedup_key": "sub1:k", "account": "sub1",
                             "symbol": "BTCUSDT", "action": "OPEN_LONG",
                             "status": "approved"})
    assert st.insert_signal({"dedup_key": "sub2:k", "account": "sub2",
                             "symbol": "BTCUSDT", "action": "OPEN_LONG",
                             "status": "approved"})
    assert len(st.iter_approved_signals("sub1")) == 1
    assert len(st.iter_approved_signals("sub2")) == 1


def test_live_uses_system_client_not_ccxt():
    """live 执行器必须复用 BinanceFuturesClient（系统已有实现），不是 ccxt。"""
    from crypto_research.clients.binance_futures import BinanceFuturesClient
    from executor import build_executor

    cfg = Config(mode="live", api_key="k", api_secret="s", allocated_capital=1000)
    ex = build_executor(cfg, None, account="default")
    assert isinstance(ex.client, BinanceFuturesClient), "未复用系统已有开仓客户端"


def test_live_executor_place_order_params(monkeypatch):
    """live 下单：开多传 positionSide=LONG，平多传 reduceOnly + 反向 side。"""
    from executor import LiveExecutor

    calls = []

    class MockClient:
        def set_leverage(self, symbol, lev):
            calls.append(("lev", symbol, lev))

        def place_order(self, **kw):
            calls.append(("order", kw))
            return {"orderId": 123}

        def get_order(self, symbol, oid):
            return {"avgPrice": "42000", "executedQty": "0.1"}

    cfg = Config(mode="live")
    st = Store(base_dir=Path(tempfile.mkdtemp()))
    ex = LiveExecutor(cfg, st, account="sub1", client=MockClient())
    res = ex.execute(Decision(True, _sig("OPEN_LONG"), qty=0.1, leverage=2))
    assert res.ok
    order = [c for c in calls if c[0] == "order"][0][1]
    assert order["side"] == "BUY"
    assert order["position_side"] == "LONG"
    assert order["reduce_only"] is False
    assert order["quantity"] == 0.1

    calls.clear()
    ex.execute(Decision(True, _sig("CLOSE_LONG"), qty=0.1, leverage=1))
    order = [c for c in calls if c[0] == "order"][0][1]
    assert order["reduce_only"] is True, "平仓必须 reduceOnly"
    assert order["side"] == "SELL", "平多必须反向 SELL"


def test_no_secret_in_repr():
    """账户名安全显示，不泄露 key。"""
    a = AccountConfig("sub1", "SUPER_SECRET_KEY", "SUPER_SECRET", 1000)
    assert "SECRET" not in a.safe_name()
    assert a.safe_name() == "sub1"
