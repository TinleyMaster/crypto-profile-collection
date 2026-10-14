-- ============================================================
-- fix_089_netflow_alert_log.sql
-- 净流异动告警记录表（冷却去重 + 审计留痕，2026-10-07）。
--
-- 背景：剂量-反应回测（workbench/backtest_netflow_threshold.py）结论——
--   「单日净流 ≥5% 市值」= 事件日标签（上币/解锁/迁移/巨鲸异动），
--   次日大概率跑输 BTC ≈2%（t≈2.2），但这是「事件日暴涨后的均值回归」，
--   **不是方向信号**。故告警语义 = 异动提示 + 「次日勿追高」风控，不做多空断言。
--   另：净流/市值占比才是可比口径，绝对金额受市值大小主导（无单调阈值）。
--
-- 用途：scan_netflow_alert.py 每次扫描命中告警后写入本表，
--   同资产在 cooldown 窗口内不重复告警；也用于审计历史告警。
-- ============================================================

CREATE TABLE IF NOT EXISTS biz.netflow_alert_log (
    alert_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id      INTEGER     NOT NULL REFERENCES core.asset(asset_id) ON DELETE CASCADE,
    window_h      INTEGER     NOT NULL,               -- 告警所用聚合窗口（小时）
    netflow_usd   NUMERIC     NOT NULL,               -- 窗口内累计净流（正=净流入）
    net_ratio     NUMERIC     NOT NULL,               -- netflow / market_cap（可比口径）
    level         TEXT        NOT NULL,               -- strong(≥5%) / watch(≥1%)
    alerted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_id, alerted_at)
);

CREATE INDEX IF NOT EXISTS idx_netflow_alert_log_asset_time
    ON biz.netflow_alert_log (asset_id, alerted_at DESC);
