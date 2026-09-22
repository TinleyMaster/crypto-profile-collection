-- fix_062: is_primary 错标桥接/包装变体治理（工单 W5，2026-09-22）
--
-- 背景：core.asset_source_map 中部分资产的 cg 映射把 Binance 桥接/包装变体
-- （binance-peg-*）错标为 is_primary=TRUE，导致 resolve_cg_coin_id（W4 3c8a9de）
-- 仍优先命中错标行 → 市值/解锁压力估值基于错误币种。
-- 例：LTC 的 primary 为 binance-peg-litecoin（市值 $44.8M vs 真实 ~$5B）。
--
-- 口径：仅修「无歧义子集」——primary 为 binance-peg-* 且存在名称/符号一致的非 primary 候选。
-- 全库 456 个「primary 名称≠资产名」的灰色地带不在此迁移处理（交数据治理侧逐批 review）。
--
-- 本迁移已对 prod 执行（2026-09-22）：受影响 9 个资产，每资产恰 1 条 primary，无遗留 binance-peg primary。
-- 幂等：WHERE 子句保证重复执行为 0 行。

BEGIN;

-- (0) 修正前快照
CREATE TEMP TABLE _w5_before ON COMMIT DROP AS
SELECT a.asset_id, a.canonical_symbol, asm.source_asset_key AS old_primary_cg_id
FROM core.asset a
JOIN core.asset_source_map asm
  ON asm.asset_id = a.asset_id AND asm.source_code = 'cg' AND asm.is_primary = TRUE
WHERE asm.source_asset_key LIKE 'binance-peg-%'
  AND EXISTS (
      SELECT 1 FROM core.asset_source_map m2
      LEFT JOIN src_cg.coin_info ci2 ON ci2.coin_id = m2.source_asset_key
      WHERE m2.asset_id = a.asset_id AND m2.source_code = 'cg' AND m2.is_primary = FALSE
        AND ((lower(ci2.name) IS NOT DISTINCT FROM lower(a.canonical_name))
             OR (upper(ci2.symbol) IS NOT DISTINCT FROM upper(a.canonical_symbol)))
  );

-- (1) 取消错标 primary（先 FALSE，满足 uq_asset_source_map_primary_true 唯一部分索引）
UPDATE core.asset_source_map asm
SET is_primary = FALSE, updated_at = NOW()
FROM _w5_before b
WHERE asm.asset_id = b.asset_id AND asm.source_code = 'cg'
  AND asm.source_asset_key = b.old_primary_cg_id AND asm.is_primary = TRUE;

-- (2) 选取正确候选并设为 primary（与 resolve_cg_coin_id 同款排序）
WITH correct AS (
    SELECT DISTINCT ON (a.asset_id)
           a.asset_id, asm.source_asset_key AS correct_cg_id
    FROM core.asset a
    JOIN core.asset_source_map asm
      ON asm.asset_id = a.asset_id AND asm.source_code = 'cg' AND asm.is_primary = FALSE
    LEFT JOIN src_cg.coin_info ci ON ci.coin_id = asm.source_asset_key
    WHERE a.asset_id IN (SELECT asset_id FROM _w5_before)
    ORDER BY a.asset_id,
        (lower(ci.name) IS NOT DISTINCT FROM lower(a.canonical_name)) DESC,
        COALESCE(upper(ci.symbol) = upper(a.canonical_symbol), false) DESC,
        COALESCE(abs(ci.market_cap_rank - a.market_cap_rank), 999999),
        asm.source_asset_key
)
UPDATE core.asset_source_map asm
SET is_primary = TRUE, match_status = 'confirmed',
    verified_by = 'w5_is_primary_governance_20260922',
    verified_at = NOW(), updated_at = NOW()
FROM correct c
WHERE asm.asset_id = c.asset_id AND asm.source_code = 'cg'
  AND asm.source_asset_key = c.correct_cg_id;

-- (3) 验证：每个受影响资产恰 1 条 primary、且无遗留 binance-peg primary
-- SELECT a.canonical_symbol, asm.source_asset_key
-- FROM core.asset a JOIN core.asset_source_map asm
--   ON asm.asset_id=a.asset_id AND asm.source_code='cg' AND asm.is_primary=TRUE
-- WHERE a.asset_id IN (SELECT asset_id FROM _w5_before);

COMMIT;
