-- ============================================================
-- doge 映射修复：清脏数据 + 补映射
-- 工单：CM扩展指标接入与doge映射修复_2026-08-30
-- ============================================================

BEGIN;

-- (a) 清历史错误脏数据：doge 全量映射到 26494（Binance-Peg Dogecoin）的行
DELETE FROM biz.cm_asset_onchain_daily
WHERE cm_symbol = 'doge' AND asset_id <> 1132;

-- (b) 补映射：real Dogecoin (asset_id=1132) → cm source_key 'doge'
INSERT INTO core.asset_source_map
  (asset_id, source_code, source_asset_key, match_status, match_method, match_confidence, is_primary)
VALUES
  (1132, 'cm', 'doge', 'confirmed', 'manual', 1.0, true)
ON CONFLICT (asset_id, source_code, source_asset_key) DO NOTHING;

COMMIT;
