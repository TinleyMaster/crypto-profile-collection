-- ============================================================
-- P1-4 CG 源跨资产污染清理（ETH 等主流币）
-- ============================================================
-- 问题：CoinGecko 的 links 字段包含跨链桥/关联项目链接（如 ETH 下有 BNB Chain、
--       Wormhole、Immutable 等其他项目官网），入库时未做资产归属过滤。
-- 影响：doc_source_entry 噪声增多，RAG 问答可能引用错误来源，资料完整性统计不准
-- 策略：
--   1. 识别 CG 来源的 official_website 中明显属于其他项目的链接
--   2. 仅删除 secondary 条目（不碰 primary）
--   3. 先从 ETH 开始清理，验证后可扩展到其他主流币
-- ============================================================

BEGIN;

-- 查看 ETH 当前 CG 来源的 official_website 条目（清理前）
-- SELECT entry_id, entry_url, is_primary, source_code, entry_type
-- FROM biz.doc_source_entry
-- WHERE entity_type = 'asset'
--   AND asset_id = (SELECT asset_id FROM core.asset WHERE canonical_symbol = 'ETH' AND asset_type = 'coin' LIMIT 1)
--   AND source_code = 'cg'
--   AND entry_type = 'official_website'
-- ORDER BY is_primary DESC, entry_id;

-- 删除 ETH 下 CG 来源的已知跨资产污染链接（仅 secondary）
DELETE FROM biz.doc_source_entry
WHERE entity_type = 'asset'
  AND asset_id = (SELECT asset_id FROM core.asset WHERE canonical_symbol = 'ETH' AND asset_type = 'coin' LIMIT 1)
  AND source_code = 'cg'
  AND entry_type = 'official_website'
  AND is_primary = false
  AND (
      -- BNB Chain 系列
      entry_url LIKE '%bnbchain.org%'
      OR entry_url LIKE '%opbnb%'
      -- StarkNet
      OR entry_url LIKE '%starkgate.starknet.io%'
      OR entry_url LIKE '%starknet.io%'
      -- Wormhole
      OR entry_url LIKE '%wormholenetwork.com%'
      OR entry_url LIKE '%wormholecrypto%'
      -- Immutable
      OR entry_url LIKE '%immutable.com%'
      OR entry_url LIKE '%immutable%explorer%'
      -- NEAR
      OR entry_url LIKE '%near-intents.org%'
      OR entry_url LIKE '%near.org%'
  );

-- 统计删除后剩余数量
DO $$
DECLARE
    v_eth_id INT;
    v_remaining INT;
    v_deleted INT;
BEGIN
    SELECT asset_id INTO v_eth_id FROM core.asset WHERE canonical_symbol = 'ETH' AND asset_type = 'coin' LIMIT 1;

    SELECT COUNT(*) INTO v_remaining
    FROM biz.doc_source_entry
    WHERE entity_type = 'asset'
      AND asset_id = v_eth_id
      AND source_code = 'cg'
      AND entry_type = 'official_website';

    RAISE NOTICE 'ETH CG official_website 剩余条目数: %', v_remaining;
END $$;

COMMIT;
