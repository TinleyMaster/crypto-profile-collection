-- 查找无任何文档入口的资产，用于 DexScreener 补充
-- 反连接改写：原 LEFT JOIN dse ... WHERE dse.entry_id IS NULL 会对
-- doc_source_entry（1.8GB / 30 万行）走全表扫描，导致每日同步卡死。
-- 改用 NOT EXISTS 走 (entity_type, asset_id) 前缀索引，实测 17x 提速。
SELECT
    a.asset_id,
    a.canonical_symbol,
    a.canonical_name,
    a.asset_type
FROM core.asset AS a
WHERE
    a.status = 'active'
    AND a.canonical_symbol IS NOT NULL
    AND a.canonical_symbol != ''
    -- 排除衍生品、合成资产、IOU 等非真实链上代币
    AND a.asset_type NOT IN ('derivative', 'synthetic', 'iou')
    AND NOT EXISTS (
        SELECT 1
        FROM biz.doc_source_entry AS dse
        WHERE dse.entity_type = 'asset'
          AND dse.asset_id = a.asset_id
    )
ORDER BY a.asset_id
LIMIT %s;
