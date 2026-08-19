-- ============================================================
-- P1 补充：全局清理 batcat.lol / alpha.wtf 污染残留
--
-- 背景：
--   fix_btc_materials_pollution.sql 只清理了 BTC 下的 batcat/alpha.wtf 污染，
--   但其他资产下仍有残留（如 asset_id=13427 下的 twitter/explorer 条目）。
--   本脚本全局扫描并清理所有资产下的已知污染域名。
--
-- 污染域名：
--   - batcat.lol       （CG homepage_url 错误，仿冒 BTC 官网）
--   - alpha.wtf        （DefiLlama 抓取的污染链接）
-- ============================================================

BEGIN;

-- ========== 修复前：统计全局污染条目 ==========
SELECT asset_id, entry_type, entry_url, source_code
FROM biz.doc_source_entry
WHERE entry_url ILIKE '%batcat.lol%'
   OR entry_url ILIKE '%alpha.wtf%'
ORDER BY asset_id, entry_type;

-- ========== 全局删除污染条目 ==========
DELETE FROM biz.doc_source_entry
WHERE entry_url ILIKE '%batcat.lol%'
   OR entry_url ILIKE '%alpha.wtf%';

-- ========== 修复后：验证清理结果 ==========
DO $$
DECLARE
    v_remaining INT;
BEGIN
    SELECT COUNT(*) INTO v_remaining
    FROM biz.doc_source_entry
    WHERE entry_url ILIKE '%batcat.lol%'
       OR entry_url ILIKE '%alpha.wtf%';

    RAISE NOTICE '清理后 batcat.lol / alpha.wtf 剩余条目数: %', v_remaining;
END $$;

COMMIT;
