-- ============================================================
-- 清理明确被污染的 tokenomics 记录
-- 只删除 source_urls 中包含已知污染域名、且置信度 < 0.6 的记录
-- 保留"信息不足但数据来自API"的低质量记录（只是不完整，不是错的）
-- ============================================================

-- 已知污染域名模式（来自非 primary 的错误官网）
-- alpha.wtf, batcat.lol, feg.io, bitgertswap.com, flap.sh 等

-- 先预览要删除的记录
SELECT a.asset_id, a.canonical_symbol, a.canonical_name,
       t.confidence, t.total_supply, t.max_supply, t.circulating_supply,
       t.source_urls
FROM biz.asset_tokenomics t
JOIN core.asset a ON a.asset_id = t.asset_id
WHERE t.confidence < 0.6
  AND (
    -- source_urls 包含明显的污染域名（与资产名称完全不符的）
    EXISTS (
        SELECT 1 FROM unnest(t.source_urls) u
        WHERE u ILIKE '%alpha.wtf%'
           OR u ILIKE '%batcat.lol%'
           OR u ILIKE '%feg.io%'
           OR u ILIKE '%bitgertswap%'
           OR u ILIKE '%flap.sh%'
           OR u ILIKE '%ape.pro%'
           OR u ILIKE '%hpop8i%'
           OR u ILIKE '%dashdapp.io%'
           OR u ILIKE '%stargate.finance%'
           OR u ILIKE '%wormholenetwork%'
           OR u ILIKE '%bridge.linea.build%'
    )
    -- 或者 notes 明确提到了污染/不匹配
    OR t.extraction_notes ILIKE '%并非%官方%'
    OR t.extraction_notes ILIKE '%可能匹配的是%'
    OR t.extraction_notes ILIKE '%大量无关%'
  );

-- 执行删除（确认上面的结果后再打开）
-- DELETE FROM biz.asset_tokenomics
-- WHERE confidence < 0.6
--   AND (
--     EXISTS (
--         SELECT 1 FROM unnest(source_urls) u
--         WHERE u ILIKE '%alpha.wtf%'
--            OR u ILIKE '%batcat.lol%'
--            OR u ILIKE '%feg.io%'
--            OR u ILIKE '%bitgertswap%'
--            OR u ILIKE '%flap.sh%'
--            OR u ILIKE '%ape.pro%'
--            OR u ILIKE '%hpop8i%'
--            OR u ILIKE '%dashdapp.io%'
--            OR u ILIKE '%stargate.finance%'
--            OR u ILIKE '%wormholenetwork%'
--            OR u ILIKE '%bridge.linea.build%'
--     )
--     OR extraction_notes ILIKE '%并非%官方%'
--     OR extraction_notes ILIKE '%可能匹配的是%'
--     OR extraction_notes ILIKE '%大量无关%'
--   );
