-- Upsert asset_source_map with primary-isolation guard.
--
-- 防回归：同一 asset_id 只能有一条 is_primary=true 的记录。
-- 当新写入 is_primary=true 时，若该 asset 已有其他源的 primary，
-- 则自动将新记录降级为 is_primary=false，避免双 primary 污染。
--
-- 优先级：cg > cmc > dl（按 8/25 既定口径，CG 为权威主映射）。
-- 实际生效由调用方传入的 is_primary 决定，本 SQL 只做互斥保护。

INSERT INTO core.asset_source_map (
    asset_id,
    source_code,
    source_asset_key,
    match_status,
    match_method,
    match_confidence,
    is_primary,
    verified_by,
    verified_at,
    updated_at
) VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    NOW(),
    NOW()
)
ON CONFLICT (source_code, source_asset_key) DO UPDATE SET
    asset_id = EXCLUDED.asset_id,
    match_status = EXCLUDED.match_status,
    match_method = EXCLUDED.match_method,
    match_confidence = EXCLUDED.match_confidence,
    -- is_primary 互斥保护：若同 asset 已有其他源的 primary，则不升级为 primary
    is_primary = CASE
        WHEN EXCLUDED.is_primary = TRUE
         AND EXISTS (
             SELECT 1 FROM core.asset_source_map
             WHERE asset_id = EXCLUDED.asset_id
               AND is_primary = TRUE
               AND (source_code, source_asset_key) <> (EXCLUDED.source_code, EXCLUDED.source_asset_key)
         )
        THEN FALSE  -- 已有 primary，降级
        ELSE EXCLUDED.is_primary
    END,
    verified_by = EXCLUDED.verified_by,
    verified_at = NOW(),
    updated_at = NOW()
RETURNING asset_id;
