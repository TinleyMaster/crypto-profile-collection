-- FIX-011: core.asset supply 污染修复 + 防回归约束
-- 审计日期：2026-08-26
-- 问题：
--   1. circulating_supply > total_supply 内部矛盾（55 个）
--   2. circulating_supply = 0 语义占位（5,712 个，应存 NULL）
--   3. 部分 meme 币 supply 量级错误（已由 sync_core_supply_from_cmc 守住）
--
-- 修复：
--   1. 0 值改 NULL（语义修正）
--   2. circ > total 的，total 向上对齐到 circ
--   3. 加 CHECK 约束防回归

-- ============================================================
-- 第一步：备份原值
-- ============================================================

CREATE TABLE IF NOT EXISTS public._bak_core_supply_fix_011_20260826 AS
SELECT asset_id, canonical_symbol, circulating_supply, total_supply
FROM core.asset
WHERE circulating_supply = 0
   OR total_supply = 0
   OR (circulating_supply IS NOT NULL
       AND total_supply IS NOT NULL
       AND circulating_supply > total_supply
       AND total_supply > 0);

-- ============================================================
-- 第二步：语义修正 —— 0 改为 NULL
-- ============================================================

-- circulating_supply = 0 → NULL（"未知"≠"0"）
UPDATE core.asset
SET circulating_supply = NULL,
    updated_at = NOW()
WHERE circulating_supply = 0;

-- total_supply = 0 → NULL
UPDATE core.asset
SET total_supply = NULL,
    updated_at = NOW()
WHERE total_supply = 0;

-- ============================================================
-- 第三步：内部一致性修复 —— circulating <= total
-- ============================================================

-- circ > total 且 total > 0 的，将 total 向上对齐到 circ
-- （宁可 total 偏大，也不破坏 circ<=total 语义）
UPDATE core.asset
SET total_supply = circulating_supply,
    updated_at = NOW()
WHERE circulating_supply IS NOT NULL
  AND total_supply IS NOT NULL
  AND circulating_supply > total_supply
  AND total_supply > 0;

-- ============================================================
-- 第四步：加 CHECK 约束防回归
-- ============================================================

-- circulating_supply <= total_supply（两者都非 NULL 时）
ALTER TABLE core.asset
    DROP CONSTRAINT IF EXISTS chk_asset_supply_order;

ALTER TABLE core.asset
    ADD CONSTRAINT chk_asset_supply_order
    CHECK (
        circulating_supply IS NULL
        OR total_supply IS NULL
        OR circulating_supply <= total_supply
    );

-- supply 不能为 0（用 NULL 表示未知）
ALTER TABLE core.asset
    DROP CONSTRAINT IF EXISTS chk_asset_supply_nonzero;

ALTER TABLE core.asset
    ADD CONSTRAINT chk_asset_supply_nonzero
    CHECK (
        (circulating_supply IS NULL OR circulating_supply > 0)
        AND (total_supply IS NULL OR total_supply > 0)
    );

-- ============================================================
-- 验证
-- ============================================================

-- 验证 1：circ > total 的数量应为 0
SELECT COUNT(*) AS circ_gt_total
FROM core.asset
WHERE circulating_supply IS NOT NULL
  AND total_supply IS NOT NULL
  AND circulating_supply > total_supply;

-- 验证 2：supply = 0 的数量应为 0
SELECT
    COUNT(*) FILTER (WHERE circulating_supply = 0) AS circ_zero,
    COUNT(*) FILTER (WHERE total_supply = 0) AS total_zero
FROM core.asset;
