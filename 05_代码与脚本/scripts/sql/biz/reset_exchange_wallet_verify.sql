-- ============================================================
-- 重置交易所钱包验证状态（清空 high 标记，方便重新验证并打昵称标签）
-- ============================================================
-- 功能：
--   1. 将所有 high 置信度的地址降回 medium（保留地址记录，不清空数据）
--   2. 清空 display_name 昵称
--   3. 移除 source 中的 ;manual_verified 标记
--   4. 只操作来源包含 auto_ 或 manual_verified 的（即自动采集后人工验证过的）
--
-- 使用方法：
--   先执行 SELECT 预览影响行数，确认没问题后再执行 UPDATE
-- ============================================================

-- ── 预览：看看有多少条会被重置 ──
SELECT
    COUNT(*) as total_high,
    COUNT(CASE WHEN source LIKE '%auto_%' THEN 1 END) as auto_sourced,
    COUNT(CASE WHEN display_name IS NOT NULL AND display_name <> '' THEN 1 END) as has_display_name
FROM biz.onchain_exchange_wallet
WHERE confidence = 'high';

-- ── 执行重置（确认没问题后再取消注释执行） ──
/*
UPDATE biz.onchain_exchange_wallet
SET
    confidence   = 'medium',
    display_name = NULL,
    source       = REGEXP_REPLACE(
                       COALESCE(source, ''),
                       ';manual_verified',
                       '',
                       'g'
                   )
WHERE confidence = 'high';
*/

-- ── 验证重置结果 ──
/*
SELECT confidence, COUNT(*) as cnt
FROM biz.onchain_exchange_wallet
GROUP BY confidence
ORDER BY confidence;
*/
