-- =====================================================================
-- fix_050: asset_market_daily 三源取数统一视图（审计 P0-1，2026-09-18）
-- =====================================================================
-- 背景：biz.asset_market_daily 主键为 (asset_id, market_date, source_code)，
-- 同一资产同日最多 3 行（cmc / cmc_historical / binance_klines），
-- 价格差最大 134 倍。任何不带 source_code 过滤的 SELECT 都会行数膨胀、
-- 取值不确定。
--
-- 方案：建立统一取数视图，源优先级 cmc > cmc_historical > binance_klines。
-- 读取端统一改写为 biz.v_asset_market_daily_primary，不要改主键/加唯一约束
-- （会破坏三源设计）。
-- =====================================================================

CREATE OR REPLACE VIEW biz.v_asset_market_daily_primary AS
SELECT DISTINCT ON (asset_id, market_date) *
FROM biz.asset_market_daily
ORDER BY
    asset_id,
    market_date,
    CASE source_code
        WHEN 'cmc'            THEN 1
        WHEN 'cmc_historical' THEN 2
        WHEN 'binance_klines' THEN 3
        ELSE 9
    END;

COMMENT ON VIEW biz.v_asset_market_daily_primary IS
    '资产日行情单源视图：按 cmc > cmc_historical > binance_klines 取每日唯一行（审计 P0-1）';
