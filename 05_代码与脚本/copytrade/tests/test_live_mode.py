"""实时模式专属回归测试——离线回放掩盖的坑全部锁在这里。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402
from daemon import CopyTradeDaemon  # noqa: E402
from source import BinanceCopySource, ReplaySource  # noqa: E402
from state_machine import Mirror, PositionStateMachine  # noqa: E402
from store import Store  # noqa: E402


def test_reconcile_never_overwrites_on_closed_history():
    """H1：position-history 全是已平仓，绝不能覆写镜像（否则净持仓被毁）。"""
    sm = PositionStateMachine()
    m = sm.get_mirror("BTCUSDT", "LONG")
    from state_machine import Lot
    m.lots.append(Lot(qty=2.5, price=42000, ts=1))
    before = m.net_qty
    closed = [
        {"symbol": "BTCUSDT", "positionSide": "LONG", "closedVolume": 999999,
         "status": "All Closed", "avgCost": 100},
    ]
    diffs = sm.reconcile_from_position(closed)
    assert m.net_qty == before, "已平仓历史不得覆写实时镜像净持仓"
    assert any("跳过对账" in d for d in diffs)


def test_reconcile_only_when_realtime_field_present():
    """确有实时净持仓字段时才校准。"""
    sm = PositionStateMachine()
    m = sm.get_mirror("ETHUSDT", "LONG")
    from state_machine import Lot
    m.lots.append(Lot(qty=3, price=3000, ts=1))
    realtime = [{"symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": 5, "avgOpenPrice": 3100}]
    diffs = sm.reconcile_from_position(realtime)
    assert abs(m.net_qty - 5) < 1e-8
    assert any("偏差" in d for d in diffs)


def test_bootstrap_requires_order_source():
    """order 源失败时绝不标记 bootstrap_done（否则永久空基线）。"""
    td = tempfile.mkdtemp()
    cfg = Config(mode="paper")
    d = CopyTradeDaemon(cfg, log=lambda m: None, base_dir=Path(td))

    class FakeSource:
        def fetch_all(self, bootstrap=False):
            from source import FetchResult
            return {
                "order": FetchResult(source="order", ok=False, error="11012005 限流"),
                "position": FetchResult(source="position", ok=True, rows=[]),
                "detail": FetchResult(source="detail", ok=True, rows=[]),
            }

        def check_health(self):
            return {"source_healthy": False, "source_reason": "测试", "leader_idle": False,
                    "leader_idle_hours": 0}

    d.source = FakeSource()  # type: ignore
    out = d.run_round()
    assert out.get("phase") == "bootstrap_failed"
    assert d.store.get_meta("bootstrap_done") is None, "order 失败不得标记完成"
    assert not d.bootstrapped


def test_bootstrap_marks_done_on_success(monkeypatch):
    """order 源成功时正确回填并标记。"""
    td = tempfile.mkdtemp()
    cfg = Config(mode="paper")
    d = CopyTradeDaemon(cfg, log=lambda m: None, base_dir=Path(td))
    order_row = {"orderTime": 1795650989796, "symbol": "BTCUSDT", "side": "BUY",
                 "positionSide": "LONG", "executedQty": 1, "avgPrice": 42000}

    class FakeSource:
        def fetch_all(self, bootstrap=False):
            from source import FetchResult
            return {
                "order": FetchResult(source="order", ok=True, rows=[order_row]),
                "position": FetchResult(source="position", ok=True, rows=[]),
                "detail": FetchResult(source="detail", ok=True, rows=[{"aumAmount": 100000}]),
            }

        def check_health(self):
            return {"source_healthy": True, "source_reason": "", "leader_idle": False,
                    "leader_idle_hours": 0}

    d.source = FakeSource()  # type: ignore
    out = d.run_round()
    assert out.get("phase") == "bootstrap"
    assert d.store.get_meta("bootstrap_done") == "1"
    assert d.store.count_raw_orders() == 1
    assert d.store.stats()["follow_signal"] == 0, "冷启动不得生成任何跟随信号"
    assert d.store.stats()["exec_order"] == 0, "冷启动不得执行任何单"


def test_kill_switch_sentinel():
    """文件哨兵可运行时熔断，不需改代码/重启。"""
    td = Path(tempfile.mkdtemp())
    cfg = Config(mode="paper")
    d = CopyTradeDaemon(cfg, log=lambda m: None, base_dir=td)
    sentinel = td / "data" / "KILL_SWITCH"
    assert d._check_kill_switch() is False
    sentinel.write_text("killed", encoding="utf-8")
    assert d._check_kill_switch() is True
    assert d.kill_switch is True
    sentinel.unlink()
    assert d._check_kill_switch() is False
    assert d.kill_switch is False


def test_rebuild_state_from_raw_orders():
    """进程重启后从 raw_order 重建镜像仓位。"""
    td = Path(tempfile.mkdtemp())
    cfg = Config(mode="paper")
    store = Store(base_dir=td)
    rows = [
        {"orderTime": 1, "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG",
         "executedQty": 2, "avgPrice": 40000},
        {"orderTime": 2, "symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG",
         "executedQty": 1, "avgPrice": 41000},
    ]
    from source import _dedup_key
    for r in rows:
        store.insert_raw_order(_dedup_key(r), r, r["orderTime"], r["symbol"])
    d = CopyTradeDaemon(cfg, log=lambda m: None, base_dir=td)
    d._rebuild_state()
    snap = d.sm.snapshot()
    assert "BTCUSDT|LONG" in snap
    assert abs(snap["BTCUSDT|LONG"]["net_qty"] - 1) < 1e-8, "FIFO 后应剩 1 BTC 多仓"


def test_session_trust_env_disabled():
    """session 必须禁用 env 代理继承，否则死代理 60459 覆盖导致 502。"""
    src = BinanceCopySource(Config(), None, logger=lambda m: None)
    assert src.session.trust_env is False


def _sig(action, pos_side):
    from state_machine import FollowSignal, SignalStatus, Confidence
    return FollowSignal(symbol="BTCUSDT", position_side=pos_side, action=action,
                        qty=1, direction=1, price=42000, order_time=1,
                        confidence=Confidence.HIGH, status=SignalStatus.READY,
                        reason="", rule="", dedup_key="k")


def test_live_params_reduce_only():
    """平/减仓必须 reduceOnly，否则币安可能反向开仓。"""
    from decision import Decision
    from executor import build_order_params
    d = Decision(True, _sig("CLOSE_LONG", "LONG"), qty=1, leverage=1)
    p = build_order_params(d, "cid1")
    assert p.get("reduceOnly") is True
    assert p.get("positionSide") == "LONG"
    assert p.get("newClientOrderId") == "cid1"


def test_live_params_both_no_position_side():
    """单向持仓 BOTH 不能传 positionSide（否则 400）。"""
    from decision import Decision
    from executor import build_order_params
    d = Decision(True, _sig("OPEN_LONG", "BOTH"), qty=1, leverage=2)
    p = build_order_params(d, "cid2")
    assert "positionSide" not in p
    assert "reduceOnly" not in p


def test_live_params_short_reduce():
    """平空仓：reduceOnly + positionSide=SHORT。"""
    from decision import Decision
    from executor import build_order_params
    d = Decision(True, _sig("REDUCE_SHORT", "SHORT"), qty=0.5, leverage=1)
    p = build_order_params(d, "cid3")
    assert p.get("reduceOnly") is True
    assert p.get("positionSide") == "SHORT"


def test_default_mode_is_paper():
    """安全底线：默认 paper，绝不静默 live。"""
    from executor import build_executor, PaperExecutor
    ex = build_executor(Config(mode="paper"), None, logger=lambda m: None)
    assert isinstance(ex, PaperExecutor)
