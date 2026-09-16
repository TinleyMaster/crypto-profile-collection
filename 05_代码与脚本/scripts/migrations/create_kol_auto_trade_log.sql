-- KOL 信号自动交易审计表（币胜操盘手日记 → 币安子账户）
-- 每个信号邮件 + 每个下单决策一行，可追溯
CREATE TABLE IF NOT EXISTS biz.kol_auto_trade_log (
    id                BIGSERIAL PRIMARY KEY,
    email_message_id  TEXT,
    received_at       TIMESTAMPTZ,
    signal_time       TIMESTAMPTZ,
    kol_name          TEXT,
    symbol            TEXT,
    direction         TEXT,               -- long / short
    entry_price       NUMERIC(20,8),
    win_rate          NUMERIC(5,2),
    raw_text          TEXT,
    decision          TEXT,               -- ordered / dry_run / skipped / error
    skip_reason       TEXT,
    binance_order_id  TEXT,
    order_side        TEXT,               -- BUY / SELL
    order_type        TEXT,               -- LIMIT / MARKET / STOP_MARKET ...
    order_qty         NUMERIC(20,8),
    order_price       NUMERIC(20,8),
    sl_pct            NUMERIC(8,2),       -- 实际生效止损距离（%）
    tp_pct            NUMERIC(8,2),       -- 实际生效止盈距离（%）
    sl_source         TEXT,               -- ai / fallback / none
    ai_reason         TEXT,               -- AI 止损止盈理由
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 兼容已存在的旧表（脚本内也会执行）
ALTER TABLE biz.kol_auto_trade_log
    ADD COLUMN IF NOT EXISTS sl_pct NUMERIC(8,2),
    ADD COLUMN IF NOT EXISTS tp_pct NUMERIC(8,2),
    ADD COLUMN IF NOT EXISTS sl_source TEXT,
    ADD COLUMN IF NOT EXISTS ai_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_kol_auto_trade_log_symbol
    ON biz.kol_auto_trade_log(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kol_auto_trade_log_mid
    ON biz.kol_auto_trade_log(email_message_id);
