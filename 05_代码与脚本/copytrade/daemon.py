"""主循环：L1 取数 → L2 状态机 → L3 决策 → L4 执行 → L5 健康监控。

用法：
  python daemon.py --once          # 跑一轮（paper 默认）
  python daemon.py --loop          # 常驻轮询（supervisor 用）
  python daemon.py --replay        # 离线回放 fixtures/*.json，不发网络
  python daemon.py --health        # 只输出双时钟健康状态（watchdog 用）

安全：默认 paper；live 需显式 CT_MODE=live + 凭证，且本程序不打印密钥。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config  # noqa: E402
from decision import DecisionEngine  # noqa: E402
from executor import build_executor  # noqa: E402
from source import BinanceCopySource, ReplaySource  # noqa: E402
from state_machine import PositionStateMachine, SignalStatus  # noqa: E402
from store import Store  # noqa: E402


def make_logger(quiet: bool = False):
    def log(msg: str) -> None:
        if not quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    return log


class CopyTradeDaemon:
    def __init__(self, cfg: Config, log=None, base_dir: Path | None = None):
        self.cfg = cfg
        self.log = log or make_logger()
        self.store = Store(dsn=cfg.database_url, base_dir=base_dir or Path(__file__).parent)
        self.sm = PositionStateMachine(self.log)
        if cfg.replay_mode:
            self.source: Any = ReplaySource(cfg, self.store, self.log)
        else:
            self.source = BinanceCopySource(cfg, self.store, self.log)
        self.decision = DecisionEngine(cfg)
        self.executor = build_executor(cfg, self.store, self.log)
        self.kill_switch = False
        self.last_reconcile_day = ""
        self.round_no = 0

    def run_round(self) -> dict[str, Any]:
        self.round_no += 1
        self.log(f"=== 第 {self.round_no} 轮开始（{self.cfg.mode} 模式）===")

        # L1
        results = self.source.fetch_all()
        for name, r in results.items():
            self.log(f"  源 {name}: {'OK' if r.ok else 'FAIL'} "
                     f"{r.elapsed_ms}ms rows={len(r.rows)} {r.error}")

        # 双时钟健康：数据源故障 ≠ 交易员静默
        health = self.source.check_health()
        self.log(f"  健康: source_healthy={health['source_healthy']} "
                 f"({health['source_reason'] or 'ok'}) / "
                 f"leader_idle={health['leader_idle']} ({health['leader_idle_hours']}h)")

        # detail 里提取 AUM（B2 映射需要），并每日对账一次
        detail = results.get("detail")
        if detail and detail.ok and detail.rows:
            aum = _extract_aum(detail.rows[0])
            if aum:
                self.decision.leader_aum = aum
                self.log(f"  交易员 AUM = {aum:.2f}")

        order = results.get("order")
        stats = {"fetched": 0, "signal": 0, "ready": 0, "approved": 0, "executed": 0, "hold": 0}

        if order and order.ok and order.rows:
            from source import _dedup_key
            # L2：只处理本轮新落库的成交（幂等）
            for r in order.rows:
                key = _dedup_key(r)
                # 检查是否已处理过（raw_order 新增才处理）
                is_new = self.store.insert_raw_order(
                    key, r, r.get("orderTime"), r.get("symbol")
                )
                if not is_new:
                    continue
                stats["fetched"] += 1
                sig = self.sm.process(r, key)
                stats["signal"] += 1
                if sig.status is SignalStatus.HOLD:
                    stats["hold"] += 1
                    self.log(f"  ⚠ HOLD {sig.symbol} {sig.rule}: {sig.reason}")
                    self.store.insert_signal({
                        "dedup_key": "hold_" + key, "order_time": sig.order_time,
                        "symbol": sig.symbol, "action": sig.action,
                        "target_notional": 0, "confidence": sig.confidence.value,
                        "status": "hold", "reason": sig.reason,
                    })
                    continue
                stats["ready"] += 1
                # L3
                exposure, sym_exposure = self._exposure(sig.symbol)
                dec = self.decision.decide(
                    sig, exposure, sym_exposure,
                    kill_switch=self.kill_switch,
                    reconciliation_bad=(not health["source_healthy"]),
                )
                self.store.insert_signal({
                    "dedup_key": key, "order_time": sig.order_time,
                    "symbol": sig.symbol, "action": sig.action,
                    "target_notional": dec.notional, "confidence": sig.confidence.value,
                    "status": "approved" if dec.approved else "rejected",
                    "reason": dec.reason,
                })
                # 关联 signal_id 供 _my_position 反查方向
                with self.store._db() as c:
                    sid = c.execute("SELECT id FROM follow_signal WHERE dedup_key=?", (key,)).fetchone()
                dec.signal_id = int(sid["id"]) if sid else None
                if not dec.approved:
                    self.log(f"  ✗ 拒单 {sig.symbol} {sig.action}: {dec.reason}")
                    continue

                # 安全补丁：减仓数量 clamp 到**我方实际持仓**（从 exec_order 累计）。
                # 注意：sm.mirror 记的是交易员仓位，不是我方仓位，两者口径不同；
                # 开仓受单笔闸门截尾后，按比例映射的减仓量可能远大于我方实际持仓，
                # 不 clamp 会下超量平仓单（实盘可能反向开仓）。
                if sig.action.startswith(("CLOSE", "REDUCE")):
                    # 用 action 后缀的方向（被平仓位方向），不是成交方向（SELL=空）
                    pos_direction = 1 if sig.action.endswith("LONG") else -1
                    available = self._my_position(sig.symbol, sig.position_side, pos_direction)
                    if available <= 1e-9:
                        self.log(f"  ✗ 拒单 {sig.symbol} {sig.action}: 我方无持仓，跳过减仓")
                        continue
                    if dec.qty > available + 1e-9:
                        self.log(f"  ⚠ 减仓 clamp {sig.symbol}: {dec.qty:.6f} → {available:.6f}（我方持仓上限）")
                        dec.qty = available
                        dec.notional = available * sig.price

                stats["approved"] += 1
                # L4
                res = self.executor.execute(dec)
                if res.ok:
                    stats["executed"] += 1
                    self.log(f"  ✓ 执行 {sig.symbol} {sig.action} qty={dec.qty:.6f} ({res.status})")
                else:
                    self.log(f"  ✗ 执行失败 {sig.symbol}: {res.reason}")

        # 每日 A2 对账（position-history 校准镜像）
        today = time.strftime("%Y-%m-%d", time.gmtime())
        pos = results.get("position")
        if pos and pos.ok and pos.rows and today != self.last_reconcile_day:
            diffs = self.sm.reconcile_from_position(pos.rows)
            self.last_reconcile_day = today
            self.log(f"  对账: {'一致' if not diffs else '; '.join(diffs)}")
            for sym, m in self.sm.snapshot().items():
                s, side = sym.split("|")
                self.store.upsert_mirror(s, side, m["net_qty"], m["avg_price"], m["realized_pnl"], "open")

        return {"round": self.round_no, "health": health, "stats": stats}

    def _my_position(self, symbol: str, side: str, direction: int) -> float:
        """我方在该品种/方向的净持仓，从 exec_order + follow_signal 累计。

        exec_order 不带方向字段，关联 follow_signal 拿 action 判方向。
        """
        with self.store._db() as c:
            rows = c.execute(
                "SELECT e.filled_qty, f.action, f.symbol "
                "FROM exec_order e LEFT JOIN follow_signal f ON e.signal_id=f.id "
                "WHERE e.status IN ('paper_filled','live_submitted')"
            ).fetchall()
        target_direction = "LONG" if direction > 0 else "SHORT"
        net = 0.0
        for r in rows:
            if r["symbol"] != symbol:
                continue
            act = (r["action"] or "")
            # action 后缀带方向（OPEN_LONG/CLOSE_SHORT 等）
            if not act.endswith("_" + target_direction):
                continue
            qty = float(r["filled_qty"] or 0)
            if act.startswith(("CLOSE", "REDUCE")):
                qty = -qty
            net += qty
        return max(0.0, net)

    def _exposure(self, symbol: str) -> tuple[float, float]:
        """从 follow_signal 已批准的开仓名义估算我方敞口。

        注意：不能用镜像仓位的 (net_qty × 对方价格)——那是交易员的名义，
        我方分配资金通常小得多，会误触发总敞口闸门。以我方批准信号的
        target_notional 为准，减仓信号扣减对应品种敞口。
        """
        with self.store._db() as c:
            rows = c.execute(
                "SELECT symbol, action, target_notional FROM follow_signal WHERE status='approved'"
            ).fetchall()
        total = 0.0
        sym_total = 0.0
        for r in rows:
            n = float(r["target_notional"] or 0)
            if r["action"].startswith(("CLOSE", "REDUCE")):
                # 平仓按比例释放：简化为扣减该品种已记录名义（保守）
                total = max(0.0, total - n)
                if r["symbol"] == symbol:
                    sym_total = max(0.0, sym_total - n)
            else:
                total += n
                if r["symbol"] == symbol:
                    sym_total += n
        return total, sym_total

    def run_once(self):
        out = self.run_round()
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    def run_loop(self):
        while True:
            try:
                self.run_round()
            except KeyboardInterrupt:
                self.log("收到中断，退出")
                break
            except Exception as e:  # noqa: BLE001
                self.log(f"✗ 轮次异常 {type(e).__name__}: {str(e)[:200]}")
                self.store.mark_fetch_fail("daemon", f"{type(e).__name__}: {str(e)[:200]}")
            time.sleep(self.cfg.health.poll_interval_sec)

    def run_replay(self):
        """离线回放全部 fixtures，逐条验证状态机，不执行真实下单。"""
        assert isinstance(self.source, ReplaySource), "replay 需要 CT_REPLAY_MODE=true"
        self.log("=== 离线回放模式（paper，无网络）===")
        while not self.source.exhausted:
            self.run_round()
        print("\n=== 回放结束：镜像仓位快照 ===")
        print(json.dumps(self.sm.snapshot(), ensure_ascii=False, indent=2))
        print("\n=== DB 统计 ===")
        print(json.dumps(self.store.stats(), ensure_ascii=False, indent=2))

    def run_health(self):
        h = self.source.check_health()
        print(json.dumps(h, ensure_ascii=False, indent=2, default=str))
        # 退出码：数据源故障=1（watchdog 据此告警）；交易员静默=0（常态，不告警）
        return 1 if not h["source_healthy"] else 0


def _extract_aum(detail: dict[str, Any]) -> float | None:
    for k in ("totalEquity", "aum", "totalAsset", "accountBalance", "totalBalance", "equity"):
        v = detail.get(k)
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    return f
            except (ValueError, TypeError):
                pass
    # 嵌套 data
    for v in detail.values():
        if isinstance(v, dict):
            return _extract_aum(v)
    return None


def main():
    ap = argparse.ArgumentParser(description="自动跟单程序 COPYTRADE-AUTO-001")
    ap.add_argument("--once", action="store_true", help="跑一轮")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--replay", action="store_true", help="离线回放 fixtures")
    ap.add_argument("--health", action="store_true", help="输出双时钟健康状态")
    args = ap.parse_args()

    cfg = Config.load()
    if args.replay:
        from dataclasses import replace
        cfg = replace(cfg, replay_mode=True)
    daemon = CopyTradeDaemon(cfg)
    if args.health:
        sys.exit(daemon.run_health())
    if args.replay:
        daemon.run_replay()
    elif args.loop:
        daemon.run_loop()
    else:
        daemon.run_once()


if __name__ == "__main__":
    main()
