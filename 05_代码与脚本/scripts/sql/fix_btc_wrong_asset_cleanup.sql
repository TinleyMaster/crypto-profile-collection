-- ============================================================
-- 催化剂错连资产脏数据清理脚本（v3 — market_cap_rank 版）
-- 背景：linker.py 旧代码 symbol→asset_id 兜底无 CMC 排名排序，
--       导致 BTC/XRP/BNB/ETH/SOL 等主流币被映射到同名小市值仿盘
--       （Bitcoin Base / XRP AI / BNBTiger Inu / NEAR Intents Bridged ETH / Sol The Trophy Tomato 等）
-- 修复逻辑：
--   1. 找出所有「同 symbol 中非 CMC 排名最小（最主流）」的错连资产
--   2. 删除这些资产在 catalyst 相关表中的脏记录
--   3. 保留每 symbol CMC 排名最靠前的那条资产关联
--   4. 清理后重跑慢通道，让新 linker 逻辑用正确资产重新生成信号
-- 使用：在容器内 psql 执行，建议先验证再 COMMIT
--   psql> \i scripts/sql/fix_btc_wrong_asset_cleanup.sql
--   psql> -- 查看 Step 2 结果，确认数量合理
--   psql> COMMIT;   -- 确认无误再提交
--   bash> python scripts/bin/phase_catalyst_pipeline.py --slow  # 重跑慢通道回灌
-- ============================================================

BEGIN;

-- ---- Step 1: 识别所有"同 symbol 中非 CMC 排名最小"的错连资产
-- （rank 越小越主流，这些是 linker 旧逻辑可能错选的资产）
CREATE TEMP TABLE wrong_assets AS
WITH ranked AS (
    SELECT asset_id, canonical_symbol, canonical_name, market_cap_rank,
           ROW_NUMBER() OVER (
               PARTITION BY UPPER(canonical_symbol)
               ORDER BY market_cap_rank ASC NULLS LAST, asset_id
           ) AS rn
    FROM core.asset
    WHERE canonical_symbol IS NOT NULL
      AND TRIM(canonical_symbol) <> ''
      -- 排除 tokenized / 合成 / 衍生品 / 商品，这些不应在加密货币信号中
      AND LOWER(COALESCE(canonical_name, '')) !~ 'tokeniz|b[[:space:]]*stocks|pre[[:space:]]*stocks|futures|derivativ|crude[[:space:]]+oil|brent'
      -- 排除桥接/包装/AI 仿盘/meme 仿盘等
      AND LOWER(COALESCE(canonical_name, '')) !~ 'bridged|wrapped|intents|\bai\b|second[[:space:]]+chance|trophy|tomato|tiger[[:space:]]+inu|\binu\b|base[[:space:]]+coin|gold[[:space:]]+ai'
)
SELECT asset_id, canonical_symbol, canonical_name, market_cap_rank
FROM ranked
WHERE rn > 1
  AND market_cap_rank IS NOT NULL  -- 只清理有 CMC 排名的（明确有更主流真币的）
ORDER BY UPPER(canonical_symbol), market_cap_rank DESC;

-- 建索引加速后续 JOIN
CREATE INDEX ON wrong_assets (asset_id);

-- ---- Step 2: 查询受影响的记录数（验证用，务必先看结果）
SELECT 'wrong_assets_total' AS step, COUNT(*) AS cnt FROM wrong_assets
UNION ALL
SELECT 'catalyst_asset_link', COUNT(*)
FROM biz.catalyst_asset_link cal
JOIN wrong_assets w ON cal.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_signal', COUNT(*)
FROM biz.catalyst_signal cs
JOIN wrong_assets w ON cs.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_second_order', COUNT(*)
FROM biz.catalyst_second_order cso
JOIN wrong_assets w ON cso.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_resonance', COUNT(*)
FROM biz.catalyst_resonance cr
JOIN wrong_assets w ON cr.asset_id = w.asset_id
ORDER BY step;

-- （可选）查看错连资产明细（前 30 条）
-- SELECT canonical_symbol, canonical_name, market_cap_rank, asset_id
-- FROM wrong_assets
-- ORDER BY market_cap_rank DESC
-- LIMIT 30;

-- ---- Step 3: 删除脏数据（级联顺序：signal → second_order → resonance → asset_link）
DELETE FROM biz.catalyst_signal cs
USING wrong_assets w
WHERE cs.asset_id = w.asset_id;

DELETE FROM biz.catalyst_second_order cso
USING wrong_assets w
WHERE cso.asset_id = w.asset_id;

DELETE FROM biz.catalyst_resonance cr
USING wrong_assets w
WHERE cr.asset_id = w.asset_id;

DELETE FROM biz.catalyst_asset_link cal
USING wrong_assets w
WHERE cal.asset_id = w.asset_id;

-- ---- Step 4: 清理后验证（应该全为 0）
SELECT 'catalyst_asset_link_after' AS step, COUNT(*) AS cnt
FROM biz.catalyst_asset_link cal
JOIN wrong_assets w ON cal.asset_id = w.asset_id
UNION ALL
SELECT 'catalyst_signal_after', COUNT(*)
FROM biz.catalyst_signal cs
JOIN wrong_assets w ON cs.asset_id = w.asset_id
ORDER BY step;

-- ============================================================
--  确认 Step 2 和 Step 4 结果合理后，手动执行：
--  COMMIT;
--  否则回滚：
--  ROLLBACK;
--
--  提交后务必重跑慢通道，让新 linker 用正确资产重新生成信号：
--    python scripts/bin/phase_catalyst_pipeline.py --slow
-- ============================================================
