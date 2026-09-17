-- ============================================================
-- fix_046_scan_execute.sql
-- 盘面异动扫描 · P4 执行层数据基础（幂等）
--   1. biz.scan_signal 增加执行状态列（exec_state / executed_at / trigger_price / stop_loss_pct）
--   2. biz.scan_auto_trade_log 执行审计表
-- ============================================================

-- 1. scan_signal 执行状态（P4 dry-run/执行去重；trigger_price/stop_loss_pct 供回填）
ALTER TABLE biz.scan_signal
    ADD COLUMN IF NOT EXISTS exec_state TEXT,          -- pending / dry_run / ordered / skipped / error
    ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ;  -- 最近一次执行处理时间

COMMENT ON COLUMN biz.scan_signal.exec_state IS 'P4 执行状态（NULL=未处理；dry_run=仅模拟；ordered=已下单；skipped/error=跳过/失败）';
COMMENT ON COLUMN biz.scan_signal.executed_at IS 'P4 最近执行处理时间';

CREATE INDEX IF NOT EXISTS idx_scan_signal_exec_pending
    ON biz.scan_signal (confidence, pool, exec_state, signal_ts)
    WHERE exec_state IS NULL;

-- 2. 执行审计表（复用 KOL 自动交易审计口径，源改为 scan_signal）
CREATE TABLE IF NOT EXISTS biz.scan_auto_trade_log (
    id               BIGSERIAL PRIMARY KEY,
    signal_id        BIGINT,                     -- biz.scan_signal.id
    signal_ts        TIMESTAMPTZ,
    symbol           TEXT,
    scenario         TEXT,
    confidence       TEXT,
    p_dir            TEXT,
    price_chg_pct    NUMERIC(10,3),
    oi_chg_pct       NUMERIC(12,3),
    trigger_price    NUMERIC(20,8),
    stop_loss_pct    NUMERIC(8,2),
    take_profit_pct  NUMERIC(8,2),
    sl_source        TEXT,                       -- ai / fallback / fixed / none
    ai_reason        TEXT,
    decision         TEXT,                       -- dry_run / ordered / skipped / error
    skip_reason      TEXT,
    binance_order_id TEXT,
    order_side       TEXT,
    order_type       TEXT,
    order_qty        NUMERIC(20,8),
    order_price      NUMERIC(20,8),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scan_auto_trade_log_signal
    ON biz.scan_auto_trade_log (signal_id);

COMMENT ON TABLE biz.scan_auto_trade_log IS '盘面异动扫描·P4 执行审计：每次 dry-run/真实执行留痕，可追溯';
