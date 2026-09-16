#!/usr/bin/env python3
"""盘面异动扫描 P0 · 清算事件订阅：Binance WebSocket !forceOrder@arr → biz.liquidation_events。

全市场强平单实时广播（免费公共流），用于 S2/S4 诱多诱空场景辅助验证。
常驻运行，自动断线重连（指数退避）；事件批量落库（每 5s 或每 100 条 flush），
按 (symbol, order_id) 幂等去重。

用法：
    python phase_liquidation_collector.py              # 常驻订阅（默认）
    python phase_liquidation_collector.py --once 5     # 收到 5 条事件后退出（冒烟）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import websocket  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
FLUSH_EVERY_S = 5
FLUSH_EVERY_N = 100
MAX_RECONNECT_DELAY = 60


def ensure_table_and_index(settings) -> None:
    """确保表与幂等唯一索引存在（迁移已建表，这里补索引防重复）。"""
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_liq_events_symbol_order
                ON biz.liquidation_events (symbol, order_id)
                """
            )


def parse_event(payload: dict) -> dict | None:
    """解析 forceOrder 事件 → 入库行；只收已成交（FILLED）强平单。"""
    o = payload.get("o") or {}
    if o.get("X") != "FILLED":
        return None
    symbol = o.get("s", "")
    order_id = o.get("i")
    if not symbol or order_id is None:
        return None
    qty = float(o.get("l") or o.get("q") or 0)       # 最近成交数量
    price = float(o.get("L") or o.get("ap") or o.get("p") or 0)
    trade_ms = o.get("T") or payload.get("E") or int(time.time() * 1000)
    return {
        "event_ts": datetime.fromtimestamp(trade_ms / 1000.0, tz=timezone.utc),
        "symbol": symbol,
        "side": o.get("S"),
        "qty": qty,
        "price": price,
        "usd_value": qty * price,
        "order_id": int(order_id),
    }


class Collector:
    def __init__(self, settings, once: int = 0):
        self.settings = settings
        self.once = once
        self.events: list[tuple] = []
        self.last_flush = time.time()
        self.count = 0

    def flush(self) -> None:
        if not self.events:
            return
        try:
            with get_connection(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO biz.liquidation_events
                            (event_ts, symbol, side, qty, price, usd_value, order_id, exchange)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,'binance')
                        ON CONFLICT (symbol, order_id) DO NOTHING
                        """,
                        self.events,
                    )
            self.events.clear()
            self.last_flush = time.time()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 落库失败（保留缓存稍后重试）: {e}", file=sys.stderr)

    def on_message(self, ws, message: str) -> None:
        try:
            row = parse_event(json.loads(message))
        except Exception:
            return
        if row is None:
            return
        self.events.append(tuple(row.values()))
        self.count += 1
        if len(self.events) >= FLUSH_EVERY_N or (time.time() - self.last_flush) >= FLUSH_EVERY_S:
            self.flush()
        if self.once and self.count >= self.once:
            self.flush()
            print(f"[collector] 冒烟完成：收到 {self.count} 条清算事件")
            ws.close()
            raise SystemExit(0)

    def on_error(self, ws, error) -> None:
        print(f"[collector] websocket 错误: {error}", file=sys.stderr)

    def on_close(self, ws, code, reason) -> None:
        print(f"[collector] 连接关闭 code={code} reason={reason}")

    def run(self) -> None:
        backoff = 1
        while True:
            try:
                print(f"[collector] 连接 {WS_URL} ...")
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                ws.run_forever(ping_interval=20, ping_timeout=10)
                backoff = 1
            except KeyboardInterrupt:
                self.flush()
                print("[collector] 退出")
                break
            except Exception as e:  # noqa: BLE001
                print(f"[collector] 异常: {e}", file=sys.stderr)
            # 断线重连（指数退避）
            self.flush()
            print(f"[collector] {backoff}s 后重连 ...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance 全市场清算事件实时订阅落库")
    parser.add_argument("--once", type=int, default=0,
                        help="收到 N 条事件后退出（冒烟测试）")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    ensure_table_and_index(settings)
    Collector(settings, once=args.once).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
