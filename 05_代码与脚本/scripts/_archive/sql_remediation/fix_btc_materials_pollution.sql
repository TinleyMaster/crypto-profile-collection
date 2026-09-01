-- ============================================================
-- P2-⑧ Bitcoin materials 次要污染清理
-- ============================================================
-- 问题：BTC 的 doc_source_entry 中混入了 batcat.lol / alpha.wtf 等非官方 secondary 官网
--   - batcat.lol 来自 CG homepage_url 字段错误
--   - alpha.wtf 来自 dl 抓取
-- 影响：is_primary=true 仍是 bitcoin.org，主显示不受影响，但污染源未根除
-- 策略：
--   1. 删除 BTC 下 entry_url 包含已知污染域名的 secondary 条目
--   2. 同步清理 raw.api_response 中对应 CG 原始数据的 homepage_url（打标污染）
-- ============================================================

BEGIN;

-- 1. 先查看 BTC 的所有 official_website 条目（确认现状）
-- SELECT entry_id, entry_url, is_primary, source_code, entry_type
-- FROM biz.doc_source_entry
-- WHERE entity_type = 'asset'
--   AND asset_id = (SELECT asset_id FROM core.asset WHERE canonical_symbol = 'BTC' AND asset_type = 'coin' LIMIT 1)
--   AND entry_type = 'official_website'
-- ORDER BY is_primary DESC, entry_id;

-- 2. 删除已知污染域名的 secondary 条目
DELETE FROM biz.doc_source_entry
WHERE entity_type = 'asset'
  AND asset_id = (SELECT asset_id FROM core.asset WHERE canonical_symbol = 'BTC' AND asset_type = 'coin' LIMIT 1)
  AND entry_type = 'official_website'
  AND is_primary = false
  AND (
      entry_url LIKE '%batcat.lol%'
      OR entry_url LIKE '%alpha.wtf%'
  );

-- 3. 统计删除数量（输出到消息）
-- （psql 下可用 GET DIAGNOSTICS，这里用 RAISE NOTICE 替代）
DO $$
DECLARE
    v_btc_id INT;
    v_deleted INT;
BEGIN
    SELECT asset_id INTO v_btc_id FROM core.asset WHERE canonical_symbol = 'BTC' AND asset_type = 'coin' LIMIT 1;

    -- 统计剩余条目
    SELECT COUNT(*) INTO v_deleted
    FROM biz.doc_source_entry
    WHERE entity_type = 'asset'
      AND asset_id = v_btc_id
      AND entry_type = 'official_website';

    RAISE NOTICE 'BTC official_website 剩余条目数: %', v_deleted;
END $$;

COMMIT;
