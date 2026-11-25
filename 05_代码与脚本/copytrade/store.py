"""存储层：原始 JSON 先落库（可重放）+ 状态游标 + 双时钟健康。

默认 SQLite（本地文件，绝不连 prod）；可选自建 Postgres（CT_DATABASE_URL）。
两套 schema 用同一份 SQL 方言子集，避免双份维护。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------- schema（SQLite 方言，Postgres 版见 sql/schema.sql）----------
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_order (
    dedup_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    order_time  BIGINT,
    symbol      TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_order_time ON raw_order(order_time);

CREATE TABLE IF NOT EXISTS raw_position (
    position_id TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_signal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key     TEXT UNIQUE,
    order_time    BIGINT,
    symbol        TEXT,
    action        TEXT,
    target_notional REAL,
    confidence    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    reason        TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mirror_position (
    symbol        TEXT NOT NULL,
    position_side TEXT NOT NULL,
    net_qty       REAL NOT NULL DEFAULT 0,
    avg_price     REAL,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, position_side)
);

CREATE TABLE IF NOT EXISTS exec_order (
    client_order_id TEXT PRIMARY KEY,
    signal_id       INTEGER,
    exchange_order_id TEXT,
    status          TEXT NOT NULL,
    filled_qty      REAL NOT NULL DEFAULT 0,
    avg_price       REAL,
    created_at      TEXT NOT NULL
);

-- 双时钟：source_* 记录「数据源健康」，leader_* 记录「交易员活跃」，互不混淆
CREATE TABLE IF NOT EXISTS sync_state (
    source              TEXT PRIMARY KEY,
    last_cursor         TEXT,
    last_success_fetch_at TEXT,   -- 数据源最后一次成功取到响应（不管有无新数据）
    last_data_at        TEXT,     -- 数据源最后一次返回有效数据
    last_trade_at       TEXT,     -- 交易员最后一次真实成交（来自数据内容）
    consecutive_fail    INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    updated_at          TEXT NOT NULL
);
"""


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class Store:
    """线程安全的本地存储。paper/replay 模式默认 SQLite。"""

    _local = threading.local()

    def __init__(self, dsn: str = "", base_dir: Path | None = None):
        self.dsn = dsn.strip()
        if self.dsn:
            raise NotImplementedError(
                "Postgres 后端留待用户自建库后接入（D5 待拍板）；"
                "当前仅启用本地 SQLite，绝不连接 prod DB。"
            )
        base = base_dir or Path(__file__).parent
        data_dir = base / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "copytrade.sqlite"
        self._lock = threading.RLock()
        with self._db() as c:
            c.executescript(SQLITE_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path), timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """with conn 只提交事务、不关连接；这里包一层确保 close。"""
        c = self._connect()
        try:
            with c:
                yield c
        finally:
            c.close()

    def close(self) -> None:
        """兼容旧调用；连接已在 _db 中逐个关闭。"""
        return None

    # ---------- raw_order ----------
    def insert_raw_order(self, dedup_key: str, payload: dict[str, Any], order_time: int | None, symbol: str | None) -> bool:
        """返回 True=新增，False=重复（幂等）。"""
        with self._lock, self._db() as c:
            try:
                c.execute(
                    "INSERT INTO raw_order(dedup_key,payload,order_time,symbol,fetched_at) VALUES(?,?,?,?,?)",
                    (dedup_key, json.dumps(payload, ensure_ascii=False), order_time, symbol, _now_iso()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def iter_new_raw_orders(self) -> Iterable[dict[str, Any]]:
        with self._lock, self._db() as c:
            for row in c.execute("SELECT dedup_key,payload,order_time,symbol FROM raw_order ORDER BY order_time,id"):
                yield {
                    "dedup_key": row["dedup_key"],
                    "payload": json.loads(row["payload"]),
                    "order_time": row["order_time"],
                    "symbol": row["symbol"],
                }

    def count_raw_orders(self) -> int:
        with self._lock, self._db() as c:
            return int(c.execute("SELECT count(*) FROM raw_order").fetchone()[0])

    # ---------- raw_position ----------
    def insert_raw_position(self, position_id: str, payload: dict[str, Any]) -> bool:
        with self._lock, self._db() as c:
            try:
                c.execute(
                    "INSERT INTO raw_position(position_id,payload,fetched_at) VALUES(?,?,?)",
                    (position_id, json.dumps(payload, ensure_ascii=False), _now_iso()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # ---------- 双时钟健康 ----------
    def get_sync(self, source: str) -> dict[str, Any]:
        with self._lock, self._db() as c:
            row = c.execute("SELECT * FROM sync_state WHERE source=?", (source,)).fetchone()
            return dict(row) if row else {}

    def mark_fetch_success(self, source: str, has_data: bool, last_trade_at: str | None = None, cursor: str | None = None) -> None:
        """数据源成功取到响应——无论有无新数据都刷新成功时钟。"""
        with self._lock, self._db() as c:
            now = _now_iso()
            row = c.execute("SELECT last_data_at FROM sync_state WHERE source=?", (source,)).fetchone()
            last_data = now if has_data else (row["last_data_at"] if row else None)
            sets = [
                "last_success_fetch_at=?",
                "consecutive_fail=0",
                "last_error=NULL",
                "updated_at=?",
            ]
            vals: list[Any] = [now, now]
            if last_trade_at:
                sets.append("last_trade_at=?")
                vals.append(last_trade_at)
            if cursor is not None:
                sets.append("last_cursor=?")
                vals.append(cursor)
            if has_data:
                sets.append("last_data_at=?")
                vals.append(last_data)
            vals.append(source)
            c.execute(f"UPDATE sync_state SET {','.join(sets)} WHERE source=?", vals)
            if c.total_changes == 0:
                c.execute(
                    "INSERT INTO sync_state(source,last_cursor,last_success_fetch_at,last_data_at,last_trade_at,"
                    "consecutive_fail,last_error,updated_at) VALUES(?,?,?,?,?,0,NULL,?)",
                    (source, cursor, now, last_data, last_trade_at, now),
                )

    def mark_fetch_fail(self, source: str, err: str) -> None:
        with self._lock, self._db() as c:
            now = _now_iso()
            row = c.execute("SELECT consecutive_fail FROM sync_state WHERE source=?", (source,)).fetchone()
            n = (row["consecutive_fail"] if row else 0) + 1
            c.execute(
                "UPDATE sync_state SET consecutive_fail=?,last_error=?,updated_at=? WHERE source=?",
                (n, err[:500], now, source),
            )
            if c.total_changes == 0:
                c.execute(
                    "INSERT INTO sync_state(source,consecutive_fail,last_error,updated_at) VALUES(?,?,?,?)",
                    (source, n, err[:500], now),
                )

    # ---------- follow_signal / exec_order ----------
    def insert_signal(self, sig: dict[str, Any]) -> bool:
        with self._lock, self._db() as c:
            try:
                c.execute(
                    "INSERT INTO follow_signal(dedup_key,order_time,symbol,action,target_notional,confidence,status,reason,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        sig["dedup_key"], sig.get("order_time"), sig.get("symbol"), sig["action"],
                        sig.get("target_notional"), sig.get("confidence", "high"),
                        sig.get("status", "pending"), sig.get("reason", ""), _now_iso(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def upsert_mirror(self, symbol: str, side: str, net_qty: float, avg_price: float | None, realized_pnl: float, status: str) -> None:
        with self._lock, self._db() as c:
            c.execute(
                "INSERT INTO mirror_position(symbol,position_side,net_qty,avg_price,realized_pnl,status,updated_at)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,position_side) DO UPDATE SET"
                " net_qty=excluded.net_qty,avg_price=excluded.avg_price,realized_pnl=excluded.realized_pnl,"
                " status=excluded.status,updated_at=excluded.updated_at",
                (symbol, side, net_qty, avg_price, realized_pnl, status, _now_iso()),
            )

    def get_mirror(self, symbol: str, side: str) -> dict[str, Any] | None:
        with self._lock, self._db() as c:
            row = c.execute(
                "SELECT * FROM mirror_position WHERE symbol=? AND position_side=?", (symbol, side)
            ).fetchone()
            return dict(row) if row else None

    def insert_exec_order(self, order: dict[str, Any]) -> bool:
        with self._lock, self._db() as c:
            try:
                c.execute(
                    "INSERT INTO exec_order(client_order_id,signal_id,exchange_order_id,status,filled_qty,avg_price,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        order["client_order_id"], order.get("signal_id"), order.get("exchange_order_id"),
                        order["status"], order.get("filled_qty", 0), order.get("avg_price"), _now_iso(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def stats(self) -> dict[str, int]:
        with self._lock, self._db() as c:
            out = {}
            for t in ("raw_order", "raw_position", "follow_signal", "mirror_position", "exec_order", "sync_state"):
                out[t] = int(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
            return out
