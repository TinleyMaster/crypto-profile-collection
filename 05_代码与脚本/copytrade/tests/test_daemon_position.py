"""锁住 daemon 层两个口径坑：我方持仓 vs 交易员镜像、成交方向 vs 仓位方向。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402
from daemon import CopyTradeDaemon  # noqa: E402


def make_daemon():
    td = Path(tempfile.mkdtemp())
    cfg = Config(mode="paper", replay_mode=True, replay_dir="fixtures")
    d = CopyTradeDaemon(cfg, base_dir=td)
    return d


def test_my_position_uses_exec_not_mirror():
    d = make_daemon()
    # 模拟：交易员镜像 1.0 BTC，但我方只成交了 0.002381（受闸门截尾）
    d.sm.process({"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG",
                  "executedQty": 1.0, "price": 42000, "orderTime": 1}, "k_mirror")
    with d.store._db() as c:
        c.execute("INSERT INTO follow_signal(id,dedup_key,symbol,action,status,created_at)"
                  " VALUES(1,'k1','BTCUSDT','OPEN_LONG','approved',datetime('now'))")
        c.execute("INSERT INTO exec_order(client_order_id,signal_id,status,filled_qty,created_at)"
                  " VALUES('c1',1,'paper_filled',0.002381,datetime('now'))")
    available = d._my_position("BTCUSDT", "LONG", 1)
    assert abs(available - 0.002381) < 1e-8, "必须用我方持仓，不是交易员镜像"


def test_my_position_direction_from_action():
    d = make_daemon()
    with d.store._db() as c:
        c.execute("INSERT INTO follow_signal(id,dedup_key,symbol,action,status,created_at)"
                  " VALUES(1,'k1','BTCUSDT','OPEN_SHORT','approved',datetime('now'))")
        c.execute("INSERT INTO exec_order(client_order_id,signal_id,status,filled_qty,created_at)"
                  " VALUES('c1',1,'paper_filled',0.5,datetime('now'))")
    # 查多方向应为 0（持仓是空），查空方向应为 0.5
    assert d._my_position("BTCUSDT", "SHORT", 1) == 0.0
    assert abs(d._my_position("BTCUSDT", "SHORT", -1) - 0.5) < 1e-8
