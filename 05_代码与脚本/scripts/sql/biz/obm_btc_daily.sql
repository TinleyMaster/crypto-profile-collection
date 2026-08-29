-- Open Bitcoin Metrics BTC 链上日频指标表（长表）
-- 数据源：github.com/openbitcoinmetrics（MIT + CC BY 4.0）
-- 数据截至 2026-08-24，纯历史分位用

CREATE TABLE IF NOT EXISTS biz.obm_btc_daily (
  metric_name     TEXT    NOT NULL,   -- obm_supply_btc_daily / obm_dormancy_days_daily / ... (23)
  metric_date     DATE    NOT NULL,
  value           NUMERIC,
  unit            TEXT,
  frequency       TEXT,
  release_version TEXT,
  source_cutoff   DATE    NOT NULL DEFAULT '2026-08-24',  -- 数据截止诚实线
  PRIMARY KEY (metric_name, metric_date)
);

CREATE INDEX IF NOT EXISTS ix_obm_metric_date ON biz.obm_btc_daily (metric_name, metric_date);

COMMENT ON TABLE biz.obm_btc_daily IS 'Open Bitcoin Metrics BTC 链上日频 23 指标（全节点重建，MIT+CC BY 4.0）；数据截至 2026-08-24，纯历史分位用';
COMMENT ON COLUMN biz.obm_btc_daily.source_cutoff IS '数据源截止日期，所有行均为 2026-08-24，严禁伪装实时';
