-- 查找无任何文档入口的资产，用于 Binance 补充
-- 按 symbol 搜索，不依赖合约地址
SELECT
    a.asset_id,
    a.canonical_symbol,
    a.canonical_name,
    a.asset_type
FROM core.asset AS a
LEFT JOIN biz.doc_source_entry AS dse
    ON dse.entity_type = 'asset'
   AND dse.asset_id = a.asset_id
WHERE
    dse.entry_id IS NULL
    AND a.status = 'active'
    AND a.canonical_symbol IS NOT NULL
    AND a.canonical_symbol != ''
    -- 排除衍生品、合成资产、IOU 等非真实链上代币
    AND a.asset_type NOT IN ('derivative', 'synthetic', 'iou')
ORDER BY a.asset_id
LIMIT %s;