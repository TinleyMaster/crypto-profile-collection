#!/usr/bin/env python3
"""盘面异动扫描 · CVD 流式采集：WebSocket aggTrade → biz.oi_cvd_snapshot（精确 CVD）。

评审 §5.1 结论：REST aggTrades 自算 CVD 结构性不可行（weight=20 + 1000条/请求），
官方推荐路径 = WebSocket 流式订阅。本脚本按此实现：
  - 分批组合流订阅（每连接 ≤60 个合约的 <symbol>@aggTrade）
  - 逐笔累加：主动买 +qty*price / 主动卖 -qty*price → 5m 桶 CVD + 总成交额
  - 桶落库：仅更新 cvd_5m_usd / vol_5m_usd 两列（与 REST 采样器 OI 列共存，WS 值优先）
  - 断线重连（指数退避）+ 每 5 分钟心跳日志（防看护误杀）

CVD 无历史源可回填：本脚本自启动时刻起持续积累（REST 采样器的有界 CVD 作为兜底降级）。

用法：
    python phase_cvd_ws_collector.py                    # 常驻（全部 24h≥500万 合约）
    python phase_cvd_ws_collector.py --limit-symbols 5  # 冒烟
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import websocket  # noqa: E402

from crypto_research.clients.binance_http import fapi_get  # noqa: E402
from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

FAPI_BASE = "https://fapi.binance.com"
WS_BASE = "wss://fstream.binance.com/stream?streams="
BATCH_SIZE = 60                # 每连接合约数（防 URL 超长）
BUCKET_SECONDS = 300           # 5 分钟桶
FLUSH_EVERY_S = 5              # 落库批量间隔
HEARTBEAT_S = 60               # 周期快照 + 心跳间隔（幂等 upsert，兼看护保活）
MAX_RECONNECT_DELAY = 60


def get_symbols(min_vol_usd: float, limit: int) -> list[str]:
    data = fapi_get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    syms = sorted(
        s["symbol"] for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    )
    if min_vol_usd:
        vol = fapi_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
        vol_map = {r["symbol"]: float(r.get("quoteVolume") or 0) for r in vol}
        syms = [s for s in syms if vol_map.get(s, 0) >= min_vol_usd]
    if limit:
        syms = syms[:limit]
    return syms


class CvdAccumulator:
    """线程安全聚合器：{symbol: {'b': bucket_ms, 'signed': float, 'total': float}}。"""

    def __init__(self, out_q: queue.Queue):
        self._acc: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._out_q = out_q

    def _bucket(self, ts_ms: int) -> int:
        return ts_ms - ts_ms % (BUCKET_SECONDS * 1000)

    def add_trade(self, symbol: str, ts_ms: int, price: float, qty: float, is_buyer_maker: bool):
        b = self._bucket(ts_ms)
        signed = (-price * qty) if is_buyer_maker else (price * qty)
        with self._lock:
            cur = self._acc.get(symbol)
            if cur is None:
                self._acc[symbol] = {"b": b, "signed": signed, "total": abs(signed)}
                return
            if cur["b"] != b:
                # 桶翻转：旧桶推入落库队列
                self._out_q.put((symbol, cur["b"], cur["signed"], cur["total"]))
                self._acc[symbol] = {"b": b, "signed": signed, "total": abs(signed)}
            else:
                cur["signed"] += signed
                cur["total"] += abs(signed)

    def flush_pending(self) -> int:
        """把所有桶当前值推入落库队列并清空（进程退出时调用）。返回推入数。"""
        n = 0
        with self._lock:
            for sym, cur in self._acc.items():
                if cur["total"] > 0:
                    self._out_q.put((sym, cur["b"], cur["signed"], cur["total"]))
                    n += 1
            self._acc.clear()
        return n

    def flush_current(self) -> int:
        """周期快照：把各桶当前完整值推入队列，不清空（upsert 幂等，可重复推送）。"""
        n = 0
        with self._lock:
            for sym, cur in self._acc.items():
                if cur["total"] > 0:
                    self._out_q.put((sym, cur["b"], cur["signed"], cur["total"]))
                    n += 1
        return n


def on_message(acc: CvdAccumulator, ws, message: str) -> None:
    try:
        msg = json.loads(message)
        d = msg.get("data") or {}
        if d.get("e") != "aggTrade":
            return
        acc.add_trade(d["s"], int(d["T"]), float(d["p"]), float(d["q"]), bool(d.get("m")))
    except Exception:
        return


def run_conn(acc: CvdAccumulator, symbols: list[str]) -> None:
    url = WS_BASE + "/".join(f"{s.lower()}@aggTrade" for s in symbols)
    backoff = 1
    while True:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_message=lambda ws, m: on_message(acc, ws, m),
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
            backoff = 1
        except KeyboardInterrupt:
            return
        except Exception as e:  # noqa: BLE001
            print(f"[ws] 连接异常: {e}", file=sys.stderr)
        print(f"[ws] 断开重连（{backoff}s），批次 {symbols[0] if symbols else '?'}...", file=sys.stderr)
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_RECONNECT_DELAY)


def db_flusher(settings, out_q: queue.Queue) -> None:
    """批量落库线程。"""
    while True:
        batch: list[tuple] = []
        deadline = time.time() + FLUSH_EVERY_S
        while time.time() < deadline:
            try:
                batch.append(out_q.get(timeout=0.5))
            except queue.Empty:
                continue
        if not batch:
            continue
        try:
            with get_connection(settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO biz.oi_cvd_snapshot (symbol, ts, exchange, oi_usd, cvd_5m_usd, cvd_1h_usd, vol_5m_usd)
                        VALUES (%s,to_timestamp(%s/1000.0),'binance',NULL,%s,NULL,%s)
                        ON CONFLICT (symbol, exchange, ts) DO UPDATE SET
                            cvd_5m_usd=EXCLUDED.cvd_5m_usd, vol_5m_usd=EXCLUDED.vol_5m_usd
                        """,
                        batch,
                    )
        except Exception as e:  # noqa: BLE001
            print(f"[db] 落库失败（保留等待下批）: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="CVD WebSocket 流式采集（精确 CVD → oi_cvd_snapshot）")
    parser.add_argument("--min-vol-usd", type=float, default=5_000_000)
    parser.add_argument("--limit-symbols", type=int, default=0)
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    symbols = get_symbols(args.min_vol_usd, args.limit_symbols)
    print(f"[cvd-ws] 订阅 {len(symbols)} 个合约，分批 {BATCH_SIZE}/连接")

    out_q: queue.Queue = queue.Queue()
    acc = CvdAccumulator(out_q)

    threads = []
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        t = threading.Thread(target=run_conn, args=(acc, batch), daemon=True)
        t.start()
        threads.append(t)

    flusher = threading.Thread(target=db_flusher, args=(settings, out_q), daemon=True)
    flusher.start()

    # 心跳 + 周期快照落库（60s 一次，幂等 upsert）+ 看护保活
    try:
        while True:
            time.sleep(HEARTBEAT_S)
            n_flush = acc.flush_current()
            print(f"[cvd-ws] 心跳 {datetime.now(timezone.utc).isoformat()} "
                  f"快照推送={n_flush} 队列={out_q.qsize()}")
    except KeyboardInterrupt:
        print(f"[cvd-ws] 退出，flush 未完成桶 {acc.flush_pending()} 个")
        time.sleep(FLUSH_EVERY_S + 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
