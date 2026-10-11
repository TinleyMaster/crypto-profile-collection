-- v2 实盘交易记录表·全量留痕扩展（2026-10-11）
-- 目的：实盘下单「所有信息」可追溯复验 + 完善策略。
--   开仓侧：完整信号快照（事件 bar / chg24 / chg1h / vr / funding / vol）、
--           下单参数（名义/杠杆/保证金/计划持仓/止盈止损比例）、开仓单原始响应、保护单原始响应；
--   平仓侧：出场单 id/原始响应、出场单 userTrades 明细（realizedPnl/commission，精确定位到本单，剔除手动单）。
-- 只记「机器人下单」：本表仅由 execute_gainers_v2.py 在下单成功后写入，手动单不入本表（origin='bot'）。
-- 幂等：ADD COLUMN IF NOT EXISTS，可重复执行；新增列均可空，无 CHECK 约束（不影响存量行）。
ALTER TABLE biz.v2_trade_log
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'bot',
    ADD COLUMN IF NOT EXISTS signal_window TEXT,
    ADD COLUMN IF NOT EXISTS scan_ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS event_bar_ts TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ref_price_usd NUMERIC(24,10),
    ADD COLUMN IF NOT EXISTS chg_24h_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS chg1h_pct NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS vr_1h NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS funding_rate NUMERIC(12,8),
    ADD COLUMN IF NOT EXISTS vol_24h_usd NUMERIC(30,2),
    ADD COLUMN IF NOT EXISTS margin_usdt NUMERIC(24,2),
    ADD COLUMN IF NOT EXISTS planned_hold_h INTEGER,
    ADD COLUMN IF NOT EXISTS sl_pct NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS tr_pct NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS tp_pct NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS entry_client_order_id TEXT,
    ADD COLUMN IF NOT EXISTS entry_resp JSONB,
    ADD COLUMN IF NOT EXISTS protective_orders JSONB,
    ADD COLUMN IF NOT EXISTS exit_order_id BIGINT,
    ADD COLUMN IF NOT EXISTS close_resp JSONB,
    ADD COLUMN IF NOT EXISTS income_detail JSONB,
    ADD COLUMN IF NOT EXISTS base_url TEXT,
    ADD COLUMN IF NOT EXISTS dry_run BOOLEAN,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- 追溯查询：按信号类型/事件 bar 复验
CREATE INDEX IF NOT EXISTS idx_v2_trade_log_event_bar ON biz.v2_trade_log (event_bar_ts);