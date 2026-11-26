-- COPYTRADE-AUTO-001 Postgres 版 schema（用户自建库时用；绝不连 prod）
-- SQLite 版见 store.py 的 SQLITE_SCHEMA，两版字段一一对应。
CREATE SCHEMA IF NOT EXISTS copytrade;

CREATE TABLE IF NOT EXISTS copytrade.raw_order (
    dedup_key   TEXT PRIMARY KEY,
    payload     JSONB NOT NULL,
    order_time  BIGINT,
    symbol      TEXT,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_raw_order_time ON copytrade.raw_order(order_time);

CREATE TABLE IF NOT EXISTS copytrade.raw_position (
    position_id TEXT PRIMARY KEY,
    payload     JSONB NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS copytrade.follow_signal (
    id             BIGSERIAL PRIMARY KEY,
    dedup_key      TEXT UNIQUE,
    order_time     BIGINT,
    symbol         TEXT,
    action         TEXT,
    target_notional NUMERIC,
    confidence     TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    reason         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS copytrade.mirror_position (
    symbol        TEXT NOT NULL,
    position_side TEXT NOT NULL,
    net_qty       NUMERIC NOT NULL DEFAULT 0,
    avg_price     NUMERIC,
    realized_pnl  NUMERIC NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, position_side)
);

CREATE TABLE IF NOT EXISTS copytrade.exec_order (
    client_order_id   TEXT PRIMARY KEY,
    signal_id         BIGINT REFERENCES copytrade.follow_signal(id),
    exchange_order_id TEXT,
    status            TEXT NOT NULL,
    filled_qty        NUMERIC NOT NULL DEFAULT 0,
    avg_price         NUMERIC,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 系统元数据：冷启动标志（bootstrap_done）、熔断状态等
CREATE TABLE IF NOT EXISTS copytrade.meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 双时钟：source_* = 数据源健康；leader_* = 交易员活跃（仅提示）
CREATE TABLE IF NOT EXISTS copytrade.sync_state (
    source                TEXT PRIMARY KEY,
    last_cursor           TEXT,
    last_success_fetch_at TIMESTAMPTZ,
    last_data_at          TIMESTAMPTZ,
    last_trade_at         TIMESTAMPTZ,
    consecutive_fail      INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
