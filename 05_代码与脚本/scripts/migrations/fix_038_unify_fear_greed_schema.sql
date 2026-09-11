-- fix_038_unify_fear_greed_schema.sql
-- 统一 biz.fear_greed_daily 的列名：
--   历史遗留：value_class（旧 ingest_fear_greed.py / workbench 读取）
--   新标准：  value_classification（fix_037 建表列名）
-- 处理：给现有表添加 value_class 列并回填，保证 workbench 读库路径可用；
--       同时保留 value_classification 供新 ingest_cmc_macro.py 使用。

-- 1. 添加 value_class 列（若不存在），并回填自 value_classification
ALTER TABLE biz.fear_greed_daily
    ADD COLUMN IF NOT EXISTS value_class VARCHAR(64);

UPDATE biz.fear_greed_daily
SET value_class = value_classification
WHERE value_class IS NULL
  AND value_classification IS NOT NULL;

-- 2. 反向：若 value_classification 为空但 value_class 有值，回填
UPDATE biz.fear_greed_daily
SET value_classification = value_class
WHERE value_classification IS NULL
  AND value_class IS NOT NULL;

-- 3. 兼容旧脚本的 source_code / fetched_at 列
ALTER TABLE biz.fear_greed_daily
    ADD COLUMN IF NOT EXISTS source_code VARCHAR(32) NOT NULL DEFAULT 'cmc',
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- 索引
CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_date
    ON biz.fear_greed_daily (metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_fear_greed_daily_value
    ON biz.fear_greed_daily (value);