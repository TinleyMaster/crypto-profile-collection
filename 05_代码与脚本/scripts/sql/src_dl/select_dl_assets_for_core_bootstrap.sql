-- Bootstrap core.asset from DefiLlama protocol data.
-- 匹配优先级（一手判据优先）：
--   Priority 1: cmc_id 精确命中（协议自带 CMC id ↔ 落点资产的 cmc 映射键）
--   Priority 2: gecko_id 精确命中（协议自带 CG id ↔ 落点资产的 cg 映射键）
--   Priority 3: symbol 相等（名称仅作并列 tie-break，不再等于「已核验」）
-- 输出 match_kind 供写入侧决定留痕（cmc / gecko / symbol / NULL=无既有资产可复用）。
--
-- 硬否决（本文件新增）：**仅靠 symbol 命中**、协议带有一手 id、而落点资产持有同一来源
-- 的映射却不含该 id —— 视为一手证据证伪，该协议不进入候选池（既不映射、也不新建资产）。
-- 依据 2026-09-23《dl 映射精度诊断与修复方案》：Tier A 的 430 条「无一手落点」行若不否决，
-- 存量清理后次日 04:00 的 run_dl_pipeline 会把错配原样写回（实测 430/430 会被重新选中）。
-- 注意口径边界：仅当落点资产**持有**同源映射且不含该 id 才否决；落点资产根本没有该来源映射
-- 属「未证伪」，按 Priority 3 降级为 candidate（避免「资产只是没被 CMC 收录」的假阳性）。
WITH ranked AS (
    SELECT
        p.protocol_id,
        p.symbol,
        p.name,
        p.description,
        p.url,
        p.category,
        p.chains,
        p.cmc_id,
        p.gecko_id,
        a_cmc.asset_id AS existing_asset_id_cmc,
        a_gecko.asset_id AS existing_asset_id_gecko,
        a_sym.asset_id AS existing_asset_id_sym,
        ROW_NUMBER() OVER (
            PARTITION BY p.protocol_id
            ORDER BY
                CASE WHEN a_cmc.asset_id IS NOT NULL THEN 1
                     WHEN a_gecko.asset_id IS NOT NULL THEN 2
                     WHEN a_sym.asset_id IS NOT NULL THEN 3
                     ELSE 4 END,
                CASE WHEN UPPER(a_sym.canonical_name) = p.name THEN 1 ELSE 2 END,
                COALESCE(a_cmc.asset_id, a_gecko.asset_id, a_sym.asset_id)
        ) AS rn
    FROM src_dl.protocol_list p
    LEFT JOIN core.asset_source_map asm
        ON asm.source_code = 'dl'
        AND asm.source_asset_key = p.protocol_id
    LEFT JOIN core.asset_source_map asm_cmc
        ON asm_cmc.source_code = 'cmc'
        AND asm_cmc.source_asset_key = p.cmc_id
    LEFT JOIN core.asset a_cmc ON a_cmc.asset_id = asm_cmc.asset_id
    LEFT JOIN core.asset_source_map asm_cg
        ON asm_cg.source_code = 'cg'
        AND asm_cg.source_asset_key = p.gecko_id
    LEFT JOIN core.asset a_gecko ON a_gecko.asset_id = asm_cg.asset_id
    -- 仅当协议有有效 symbol 时才做 symbol 匹配，避免 '-'/NULL 撞车到同一兜底资产
    LEFT JOIN core.asset a_sym ON UPPER(a_sym.canonical_symbol) = UPPER(p.symbol)
        AND NULLIF(TRIM(p.symbol), '') IS NOT NULL
        AND p.symbol <> '-'
    WHERE asm.asset_id IS NULL  -- not yet mapped to core
      -- 排除实体类协议（交易所/链/钱包等非代币协议，避免 TVL 串到代币资产）
      AND COALESCE(p.category, '') NOT IN ('CEX', 'Chain', 'Wallets', 'Exchange', 'Launchpad')
      -- 无有效 symbol 且无 cmc_id 的协议不进池（无符号无法可靠匹配，避免创建垃圾资产）
      AND (
          (NULLIF(TRIM(p.symbol), '') IS NOT NULL AND p.symbol <> '-')
          OR p.cmc_id IS NOT NULL
      )
),
picked AS (
    SELECT * FROM ranked WHERE rn = 1
),
landed AS (
    SELECT
        pk.*,
        COALESCE(
            pk.existing_asset_id_cmc,
            pk.existing_asset_id_gecko,
            pk.existing_asset_id_sym
        ) AS existing_asset_id,
        CASE
            WHEN pk.existing_asset_id_cmc IS NOT NULL THEN 'cmc'
            WHEN pk.existing_asset_id_gecko IS NOT NULL THEN 'gecko'
            WHEN pk.existing_asset_id_sym IS NOT NULL THEN 'symbol'
        END AS match_kind
    FROM picked pk
)
SELECT
    protocol_id,
    symbol,
    name,
    description,
    url,
    category,
    chains,
    cmc_id,
    gecko_id,
    existing_asset_id,
    match_kind
FROM landed
WHERE NOT (
    match_kind = 'symbol'
    AND (
        (
            cmc_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM core.asset_source_map m
                WHERE m.asset_id = existing_asset_id
                  AND m.source_code = 'cmc'
            )
            AND NOT EXISTS (
                SELECT 1 FROM core.asset_source_map m
                WHERE m.asset_id = existing_asset_id
                  AND m.source_code = 'cmc'
                  AND m.source_asset_key = cmc_id
            )
        )
        OR (
            gecko_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM core.asset_source_map m
                WHERE m.asset_id = existing_asset_id
                  AND m.source_code = 'cg'
            )
            AND NOT EXISTS (
                SELECT 1 FROM core.asset_source_map m
                WHERE m.asset_id = existing_asset_id
                  AND m.source_code = 'cg'
                  AND m.source_asset_key = gecko_id
            )
        )
    )
)
LIMIT %s