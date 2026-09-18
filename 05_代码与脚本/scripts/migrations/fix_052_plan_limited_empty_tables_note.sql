-- =====================================================================
-- fix_052: 说明 CMC 付费计划受限的空表（审计 P1-3，2026-09-18）
-- =====================================================================
-- 决策：保留表与采集/消费代码，不删除。
--   biz.altcoin_season_daily / biz.asset_trending 长期 0 行并非调度或代码缺陷，
--   而是 CMC 对应端点（/v1/altcoin-season-index、/v1/cryptocurrency/trending/*）
--   需要付费 pro 计划；当前套餐下采集端 _safe_fetch 静默降级、任务 status=success
--   但 total_items=0。套餐升级后即可自动恢复，故仅补充注释避免后续审计误判。
-- =====================================================================

COMMENT ON TABLE biz.altcoin_season_daily IS
    '山寨季指数（CMC /v1/altcoin-season-index）。需 CMC pro 计划；当前套餐返回空导致长期 0 行，非缺陷，保留待套餐升级（审计 P1-3，2026-09-18）。';

COMMENT ON TABLE biz.asset_trending IS
    'CMC 趋势榜（gainers/losers/trending/most_visited/new）。需 CMC pro 计划；当前套餐拉取为空导致长期 0 行，非缺陷，保留待套餐升级（审计 P1-3，2026-09-18）。';
