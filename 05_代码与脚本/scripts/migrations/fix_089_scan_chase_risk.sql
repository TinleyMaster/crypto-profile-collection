-- =====================================================================
-- fix_089_scan_chase_risk.sql
-- 追涨风险告警表（盘面扫描 · 拥挤追涨高危组合）
--
-- 回测依据：《涨幅榜冲高回落回测方案_2026-10-03.md》Part C · 币安永续版
--   - 涨幅 ≥20% + 正资金费率（拥挤多头）：次日跌概率 62~64%（vs 近零费率 48%，差 ~15pp）；
--   - 放量冲榜更差（高成交额档 7 日跌概率 54% vs 低档 40%）。
-- ⇒ 组合「24h 涨幅 ≥20% + 正资金费率 + 放量」为短期追涨高危，扫出后告警提示规避。
--
-- 独立于 biz.scan_signal（避免污染主池/蓄势池信号的既有消费链路）：
--   信号类型 = 'chase_risk'，由 scan_chase_risk.py 写入。
-- =====================================================================

CREATE TABLE IF NOT EXISTS biz.scan_chase_risk (
    id            BIGSERIAL PRIMARY KEY,
    alert_ts      TIMESTAMPTZ    NOT NULL,          -- 告警触发时间
    symbol        TEXT           NOT NULL,          -- Binance USDT 永续合约符号
    price_usd     NUMERIC(24,8),                    -- 触发时价格
    chg_24h_pct   NUMERIC(8,2),                     -- 24h 涨幅（%）
    funding_rate  NUMERIC(12,8),                    -- 当前资金费率（正=拥挤多头）
    vol_24h_usd   NUMERIC(24,2),                    -- 24h 成交额（USDT）
    vol_ratio_7d  NUMERIC(8,2),                     -- 24h 成交额 / 近 7 日日均（放量倍数，缺数据为 NULL）
    risk_level    TEXT           NOT NULL,          -- high / medium
    reason        TEXT[],                           -- 命中原因标签（chg_ge20 / funding_positive / volume / extreme）
    emailed_at    TIMESTAMPTZ,                      -- 邮件发送时间（NULL=未发）
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scan_chase_risk_ts
    ON biz.scan_chase_risk (alert_ts DESC);
CREATE INDEX IF NOT EXISTS idx_scan_chase_risk_sym_ts
    ON biz.scan_chase_risk (symbol, alert_ts DESC);

COMMENT ON TABLE biz.scan_chase_risk IS
    '追涨风险告警（币安永续：涨幅≥20% + 正资金费率 + 放量 = 拥挤追涨高危，回测依据见涨幅榜冲高回落方案 Part C）';
