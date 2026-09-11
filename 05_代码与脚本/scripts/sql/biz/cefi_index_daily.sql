-- CEFI 指数日频数据
-- 来源：cryptoetf.today API v1/index/cefi/history
-- 用途：CEFI 指数历史分位、情绪维度辅助判断

CREATE TABLE IF NOT EXISTS biz.cefi_index_daily (
    metric_date    DATE           NOT NULL PRIMARY KEY,
    value          NUMERIC(12,4)  NOT NULL,
    source_code    VARCHAR(20)    NOT NULL DEFAULT 'cryptoetf',
    fetched_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cefi_index_daily_date ON biz.cefi_index_daily(metric_date DESC);

COMMENT ON TABLE biz.cefi_index_daily IS 'CEFI 指数日频数据（cryptoETF），中心化金融板块综合指数';
