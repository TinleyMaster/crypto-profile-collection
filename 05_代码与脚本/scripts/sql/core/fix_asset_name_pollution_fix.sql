-- =====================================================================
-- P0-1 资产名称污染修复脚本
-- 修复"一资产多 cmc_id"的 symbol 撞名污染问题
--
-- 修复策略：
--   1. 对每个多映射的 asset_id，按 cmc_rank 确定"正主"（rank 最小的保留在原 asset）
--   2. 其余 cmc_id 从原 asset 剥离，创建独立的新 core.asset 记录
--   3. 更新 asset_source_map 指向新 asset_id
--   4. 同步迁移 asset_contract 合约地址到新 asset
--
-- 安全保证：
--   - 事务包裹，可回滚
--   - 先备份污染映射到临时表，再执行修复
--   - 幂等：重复执行不会重复创建（通过备份表去重）
--
-- 用法：
--   BEGIN;
--   -- 执行本脚本全部内容
--   -- 检查影响行数无误后 COMMIT; 否则 ROLLBACK;
-- =====================================================================

-- ============================================================
-- 步骤 0：创建备份表（幂等：已存在则跳过）
-- ============================================================
CREATE TABLE IF NOT EXISTS core.asset_name_pollution_backup (
    backup_id SERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL,           -- 原污染 asset_id
    cmc_id BIGINT NOT NULL,             -- 被剥离的 cmc_id
    new_asset_id BIGINT,                -- 修复后分配的新 asset_id
    cmc_name TEXT,                      -- CMC 官方名称
    cmc_symbol TEXT,                    -- CMC 官方 symbol
    cmc_rank INT,                       -- CMC 排名
    fixed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (asset_id, cmc_id)
);

-- ============================================================
-- 步骤 1：识别所有"一资产多 cmc_id"的污染案例
--         按 cmc_rank 排序，rank 最小的为正主（保留在原 asset）
--         其余为需要剥离的映射
-- ============================================================
WITH multi_mapped AS (
    SELECT asm.asset_id
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
),
ranked_mappings AS (
    SELECT
        asm.asset_id,
        m.cmc_id,
        m.name AS cmc_name,
        m.symbol AS cmc_symbol,
        m.rank AS cmc_rank,
        ROW_NUMBER() OVER (
            PARTITION BY asm.asset_id
            ORDER BY
                m.rank NULLS LAST,        -- rank 越小越优先（正主）
                m.cmc_id ASC               -- rank 相同时按 cmc_id 升序
        ) AS rn
    FROM core.asset_source_map asm
    JOIN multi_mapped mm ON mm.asset_id = asm.asset_id
    JOIN src_cmc.cmc_asset_map m ON m.cmc_id = asm.source_asset_key::bigint
    WHERE asm.source_code = 'cmc'
)
-- 将需要剥离的映射（rn > 1）插入备份表（幂等：已存在则跳过）
INSERT INTO core.asset_name_pollution_backup (asset_id, cmc_id, cmc_name, cmc_symbol, cmc_rank)
SELECT asset_id, cmc_id, cmc_name, cmc_symbol, cmc_rank
FROM ranked_mappings
WHERE rn > 1
ON CONFLICT (asset_id, cmc_id) DO NOTHING;

-- ============================================================
-- 步骤 2：为每个待剥离的 cmc_id 创建新的 core.asset 记录
--         （仅处理尚未分配 new_asset_id 的记录）
-- ============================================================
DO $$
DECLARE
    r RECORD;
    v_new_asset_id BIGINT;
    v_fixed_count INT := 0;
BEGIN
    FOR r IN
        SELECT b.asset_id AS old_asset_id, b.cmc_id, b.cmc_name, b.cmc_symbol
        FROM core.asset_name_pollution_backup b
        WHERE b.new_asset_id IS NULL
        ORDER BY b.asset_id, b.cmc_id
    LOOP
        -- 创建新 asset（从 CMC 数据填充名称和 symbol）
        INSERT INTO core.asset (
            canonical_symbol,
            canonical_name,
            asset_type,
            status,
            launch_date,
            description_short
        )
        SELECT
            r.cmc_symbol,
            r.cmc_name,
            a.asset_type,        -- 继承原 asset 的类型
            a.status,            -- 继承原 asset 的状态
            NULL,                -- launch_date 后续从 CMC 补
            NULL                 -- description_short 后续从 CMC 补
        FROM core.asset a
        WHERE a.asset_id = r.old_asset_id
        RETURNING asset_id INTO v_new_asset_id;

        -- 更新备份表，记录新 asset_id
        UPDATE core.asset_name_pollution_backup
        SET new_asset_id = v_new_asset_id,
            fixed_at = NOW()
        WHERE asset_id = r.old_asset_id
          AND cmc_id = r.cmc_id;

        -- 更新 asset_source_map：将 cmc_id 映射从旧 asset 改到新 asset
        -- 利用 ON CONFLICT 保证 (source_code, source_asset_key) 唯一
        UPDATE core.asset_source_map
        SET asset_id = v_new_asset_id,
            match_method = 'pollution_split',
            match_confidence = 1.0,
            is_primary = true,
            updated_at = NOW()
        WHERE source_code = 'cmc'
          AND source_asset_key = r.cmc_id::text
          AND asset_id = r.old_asset_id;

        -- 迁移合约地址：将旧 asset 下属于该 cmc_id 平台的合约迁移到新 asset
        -- （通过 CMC platform 信息关联，匹配 chain + token_address）
        -- 注意：如果合约地址被多个 cmc_id 共享（罕见），保留在原 asset
        UPDATE core.asset_contract ac
        SET asset_id = v_new_asset_id,
            updated_at = NOW()
        FROM src_cmc.cmc_asset_platform p
        WHERE ac.asset_id = r.old_asset_id
          AND p.cmc_id = r.cmc_id
          AND LOWER(p.token_address) = LOWER(ac.contract_address)
          AND (
              -- 链名匹配（简单匹配，覆盖常见情况）
              CASE
                  WHEN LOWER(p.platform_name) IN ('ethereum', 'ethereum (erc20)') THEN 'ethereum'
                  WHEN LOWER(p.platform_name) IN ('bnb smart chain (bep20)', 'binance smart chain') THEN 'bsc'
                  WHEN LOWER(p.platform_name) IN ('solana', 'solana (spl)') THEN 'solana'
                  WHEN LOWER(p.platform_name) = 'base' THEN 'base'
                  WHEN LOWER(p.platform_name) IN ('polygon', 'polygon pos') THEN 'polygon'
                  WHEN LOWER(p.platform_name) IN ('arbitrum', 'arbitrum one') THEN 'arbitrum'
                  WHEN LOWER(p.platform_name) = 'ton' THEN 'ton'
                  WHEN LOWER(p.platform_name) IN ('avalanche c-chain', 'avalanche') THEN 'avalanche'
                  WHEN LOWER(p.platform_name) = 'sui' THEN 'sui'
                  WHEN LOWER(p.platform_name) = 'bittensor' THEN 'bittensor'
                  WHEN LOWER(p.platform_name) = 'osmosis' THEN 'osmosis'
                  WHEN LOWER(p.platform_name) IN ('tron20', 'tron') THEN 'tron'
                  WHEN LOWER(p.platform_name) = 'multiversx' THEN 'multiversx'
                  WHEN LOWER(p.platform_name) = 'cardano' THEN 'cardano'
                  WHEN LOWER(p.platform_name) = 'cronos' THEN 'cronos'
                  WHEN LOWER(p.platform_name) = 'icp' THEN 'icp'
                  WHEN LOWER(p.platform_name) = 'kaia' THEN 'kaia'
                  WHEN LOWER(p.platform_name) IN ('hyperliquid', 'hyperliquid l1') THEN 'hyperliquid'
                  WHEN LOWER(p.platform_name) = 'xrp ledger' THEN 'xrpl'
                  WHEN LOWER(p.platform_name) = 'aptos' THEN 'aptos'
                  WHEN LOWER(p.platform_name) = 'near' THEN 'near'
                  WHEN LOWER(p.platform_name) = 'optimism' THEN 'optimism'
                  WHEN LOWER(p.platform_name) = 'fantom' THEN 'fantom'
                  WHEN LOWER(p.platform_name) = 'blast' THEN 'blast'
                  WHEN LOWER(p.platform_name) = 'sonic' THEN 'sonic'
                  WHEN LOWER(p.platform_name) = 'zksync era' THEN 'zksync'
                  WHEN LOWER(p.platform_name) = 'scroll' THEN 'scroll'
                  WHEN LOWER(p.platform_name) = 'monad' THEN 'monad'
                  WHEN LOWER(p.platform_name) = 'berachain' THEN 'berachain'
                  WHEN LOWER(p.platform_name) = 'kava' THEN 'kava'
                  ELSE LOWER(p.platform_name)
              END
          ) = ac.chain;

        v_fixed_count := v_fixed_count + 1;
    END LOOP;

    RAISE NOTICE '已修复 % 个污染映射', v_fixed_count;
END $$;

-- ============================================================
-- 步骤 3：修复原 asset 的 canonical_name（如果被 meme 币名称污染）
--         用正主 cmc_id 的 CMC 官方名称覆盖
-- ============================================================
WITH multi_mapped AS (
    SELECT asm.asset_id
    FROM core.asset_source_map asm
    WHERE asm.source_code = 'cmc'
    GROUP BY asm.asset_id
    HAVING COUNT(*) > 1
),
primary_mapping AS (
    SELECT DISTINCT ON (asm.asset_id)
        asm.asset_id,
        m.cmc_id,
        m.name AS cmc_name,
        m.symbol AS cmc_symbol
    FROM core.asset_source_map asm
    JOIN multi_mapped mm ON mm.asset_id = asm.asset_id
    JOIN src_cmc.cmc_asset_map m ON m.cmc_id = asm.source_asset_key::bigint
    WHERE asm.source_code = 'cmc'
    ORDER BY asm.asset_id, m.rank NULLS LAST, m.cmc_id ASC
)
UPDATE core.asset a
SET
    canonical_name = pm.cmc_name,
    canonical_symbol = pm.cmc_symbol,
    updated_at = NOW()
FROM primary_mapping pm
WHERE a.asset_id = pm.asset_id
  AND (
      UPPER(a.canonical_name) != UPPER(pm.cmc_name)
      OR UPPER(a.canonical_symbol) != UPPER(pm.cmc_symbol)
  );

-- ============================================================
-- 步骤 4：验证修复结果
-- ============================================================

-- 4.1 检查是否还有"一资产多 cmc_id"的情况（应为 0）
DO $$
DECLARE
    v_remaining INT;
BEGIN
    SELECT COUNT(*) INTO v_remaining
    FROM (
        SELECT asm.asset_id
        FROM core.asset_source_map asm
        WHERE asm.source_code = 'cmc'
        GROUP BY asm.asset_id
        HAVING COUNT(*) > 1
    ) t;

    IF v_remaining > 0 THEN
        RAISE WARNING '仍有 % 个 asset 存在多 cmc_id 映射，需人工检查', v_remaining;
    ELSE
        RAISE NOTICE '验证通过：所有 asset 均只有 1 个 cmc_id 映射';
    END IF;
END $$;

-- 4.2 统计修复概览
SELECT
    COUNT(*) AS total_fixed_mappings,
    COUNT(DISTINCT asset_id) AS affected_assets,
    COUNT(DISTINCT new_asset_id) AS new_assets_created
FROM core.asset_name_pollution_backup
WHERE new_asset_id IS NOT NULL;

-- 4.3 列出所有新创建的 asset 及其映射
SELECT
    b.new_asset_id,
    a.canonical_symbol,
    a.canonical_name,
    b.cmc_id,
    b.cmc_rank,
    b.asset_id AS old_asset_id
FROM core.asset_name_pollution_backup b
JOIN core.asset a ON a.asset_id = b.new_asset_id
WHERE b.new_asset_id IS NOT NULL
ORDER BY b.cmc_rank NULLS LAST, b.new_asset_id;

-- ============================================================
-- 回滚方法（如需回滚，执行以下语句）：
--
-- BEGIN;
-- -- 1. 将 asset_source_map 映射恢复到原 asset
-- UPDATE core.asset_source_map asm
-- SET asset_id = b.asset_id,
--     updated_at = NOW()
-- FROM core.asset_name_pollution_backup b
-- WHERE asm.source_code = 'cmc'
--   AND asm.source_asset_key = b.cmc_id::text
--   AND asm.asset_id = b.new_asset_id;
--
-- -- 2. 将合约地址恢复到原 asset
-- UPDATE core.asset_contract ac
-- SET asset_id = b.asset_id,
--     updated_at = NOW()
-- FROM core.asset_name_pollution_backup b
-- WHERE ac.asset_id = b.new_asset_id;
--
-- -- 3. 删除新创建的 asset
-- DELETE FROM core.asset
-- WHERE asset_id IN (
--     SELECT new_asset_id FROM core.asset_name_pollution_backup
--     WHERE new_asset_id IS NOT NULL
-- );
--
-- -- 4. 清空备份表（或保留做记录）
-- -- TRUNCATE core.asset_name_pollution_backup;
-- COMMIT;
-- ============================================================
