-- ============================================================
-- RC-1: 价格尖刺校验 - 添加 is_anomaly 列
-- ============================================================

BEGIN;

-- 1. 为 src_cmc.cmc_asset_quote_snapshot 添加 is_anomaly 列
ALTER TABLE src_cmc.cmc_asset_quote_snapshot
ADD COLUMN IF NOT EXISTS is_anomaly BOOLEAN DEFAULT FALSE;

-- 2. 为 biz.asset_market_daily 添加 is_anomaly 列（ETL 层使用）
ALTER TABLE biz.asset_market_daily
ADD COLUMN IF NOT EXISTS is_anomaly BOOLEAN DEFAULT FALSE;

-- 3. 创建索引便于查询异常记录
CREATE INDEX IF NOT EXISTS idx_quote_snapshot_anomaly 
ON src_cmc.cmc_asset_quote_snapshot(is_anomaly) 
WHERE is_anomaly = TRUE;

CREATE INDEX IF NOT EXISTS idx_market_daily_anomaly 
ON biz.asset_market_daily(is_anomaly) 
WHERE is_anomaly = TRUE;

-- 4. 验证
SELECT 'cmc_asset_quote_snapshot' AS table_name, 
       COUNT(*) FILTER (WHERE is_anomaly = TRUE) AS anomaly_count,
       COUNT(*) AS total_count
FROM src_cmc.cmc_asset_quote_snapshot
UNION ALL
SELECT 'asset_market_daily' AS table_name,
       COUNT(*) FILTER (WHERE is_anomaly = TRUE) AS anomaly_count,
       COUNT(*) AS total_count
FROM biz.asset_market_daily;

COMMIT;