-- v2 实盘交易记录表：记录完整开→平→盈亏生命周期，供实盘胜率复验（2026-10-07）
-- 开仓由 execute_gainers_v2.py 下单成功后写入；平仓由 --watch 模式对账回填
--   （TRAIL/SL/TP 触发后币安自动平仓，执行器轮询 get_position_risk + income 补录）
-- 复验：reconcile_trades.py 输出实盘胜率，与回测（A=71.1% / B右上角=66.2%）对比
CREATE TABLE IF NOT EXISTS biz.v2_trade_log (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT NOT NULL REFERENCES biz.scan_gainer_signal(id),
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,            -- SHORT_LONG / MID_LONG / TRAP_SHORT
    direction TEXT NOT NULL,              -- LONG / SHORT
    open_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    open_price NUMERIC(24,10),            -- 开仓成交价（市价单按订单均价）
    open_qty NUMERIC(30,8),
    notional_usdt NUMERIC(24,2),
    leverage INTEGER,
    order_id BIGINT,
    close_ts TIMESTAMPTZ,
    close_price NUMERIC(24,10),
    realized_pnl_usdt NUMERIC(24,8),      -- 已实现盈亏（含手续费，币安 REALIZED_PNL+COMMISSION）
    commission_usdt NUMERIC(24,8),
    exit_reason TEXT,                     -- trail/sl/tp/expiry/unknown
    win BOOLEAN,                          -- realized_pnl_usdt > 0
    UNIQUE (signal_id)
);
CREATE INDEX IF NOT EXISTS idx_v2_trade_log_close
    ON biz.v2_trade_log (close_ts) WHERE close_ts IS NULL;
CREATE INDEX IF NOT EXISTS idx_v2_trade_log_sig
    ON biz.v2_trade_log (signal_id);
